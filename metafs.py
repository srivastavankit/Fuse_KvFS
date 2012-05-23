import os, sys, stat, errno, time

import fuse
import logging
from bsddb_wrapper import *
from mongo_wrapper import *
from helper import * 
from item import Item 
import pycrypt # WITHOUT HEADER IMPLEMENTATION


'''---------------------Actual Metafs class --------------------------------'''
class MetaFS:
    #CLASS Variables
    wbuff = ""
    PAGESIZE = 4096
    HEADER = 93
     
    def __init__(self, **kwargs):
        '''initialize and insert root entry'''
        LOGFILE = kwargs['logfile']
        logging.basicConfig(filename=LOGFILE,level=logging.DEBUG)
        logging.info("Initializing MetaFS")

        self.uid = os.getuid()
        self.gid = os.getgid()
        self.defaultMode = kwargs['defaultMode']
        #initialize data and metadata stores
        self.kv = bsddbWrapper(kwargs['db_loc'])
        self.mongo = mongoWrapper(kwargs['meta_host'], kwargs['meta_port'])
        
        item = Item(self.defaultMode | stat.S_IFDIR, self.uid, self.gid)
        
        self.mongo.addFile('/', item.getMetadata())
        self.kv.addFile('/', item.getData())
        self.kv.commit()

    # --- Metadata -----------------------------------------------------------
    def getattr(self, path):
	'''Get attributes, returns error if path(key) does not exist'''
        logging.info("Getting attributes for path"+path)
        #logging.info("Initial dict is "+str(self.mongo.get(path)))
        item = getItem(self.mongo.get(path))
        if item == None:
            return -errno.ENOENT
        st = zstat(fuse.Stat())
        st.st_mode = item.mode
        st.st_uid = item.uid
        st.st_gid = item.gid
        st.st_dev = item.dev
        st.st_atime = item.atime
        st.st_mtime = item.mtime
        st.st_ctime = item.ctime
        st.st_size = item.datalen
        return st

    def chmod(self, path, mode):
        item = getItem(self.mongo.get(path))
        if item is None:
            return None
        item.mode = mode
        self.mongo.updateFile(path,item.getMetadata())

    def chown(self, path, uid, gid):
        item = getItem(self.mongo.get(path))
        if item is None:
            return
        item.uid = uid
        item.gid = gid
        self.mongo.updateFile(path, item.getMetadata())
        
    def utime(self, path, times):
        item = getItem(self.mongo.get(path))
        if item == None:
            return
        item.ctime = item.mtime = times[0]
        self.mongo.updateFile(path,item.getMetadata())

    # --- Namespace ----------------------------------------------------------
    def unlink(self, path):
	'''Removes the file and also remove the entry in parent'''
        logging.info("Remove link")
        self._remove_from_parent_dir(path)
        self.mongo.deleteFile(path)
        self.kv.deleteFile(path)
        self.kv.commit()

    def rename(self, oldpath, newpath):
	'''Replace the oldfilename(key) with new file name'''
        logging.info("Renaming File")
        item = getItem(self.mongo.get(oldpath))
        data = self.kv.get(oldpath)
        self.mongo.deleteFile(oldpath)
        self.kv.deleteFile(oldpath)
        self.mongo.updateFile(newpath,item.getMetadata())
        self.kv.updateFile(newpath,data)
        self.kv.commit()


    # --- Links --------------------------------------------------------------
    def symlink(self, path, newpath):
	'''Create sym link where data is the path of link'''
        logging.info("Creating symbolic link")
        item = Item(self.defaultMode | stat.S_IFLNK, self.uid, self.gid)
        item.datalen = len(path)
        item.data = path
        self.mongo.addFile(newpath,item.getMetadata())
        self.kv.addFile(newpath,item.getData())
        self.kv.commit()
        self._add_to_parent_dir(newpath)

    def readlink(self, path):
        #TODO: replace metadata with data.
        logging.info("Reading link")
        return self.mongo.get(path)

    # --- Extra Attributes ---------------------------------------------------
    def setxattr(self, path, name, value, flags):
        item = self.mongo.get(path)
        if item == None:
            return
        item.xattr[name] = value
        self.mongo.updateFile(path,item)

    def getxattr(self, path, name, size):
        item = getItem(self.mongo.get(path))
        if item == None:
            return
        value = item.xattr['name']
        if size == 0:   # We are asked for size of the value
            return len(value)
        return value

    def listxattr(self, path, size):
        item = getItem(self.mongo.get(path))
        if item == None:
            return
        attrs = item.xattr.keys()
        if size == 0:
            return len(attrs) + len(''.join(attrs))
        return attrs

    def removexattr(self, path, name):
        item = getItem(self.mongo.get(path))
        if item == None:
            return
        xattrs=item.xattr
        if name in xattrs:
            del name
        item.xattr=xattrs
        self.mongo.updateFile(path,item.getMetadata())


    # --- Files --------------------------------------------------------------
    def mknod(self, path, mode, dev):
        logging.info("Create mknod at path "+path)
        item = Item(mode | stat.S_IFREG, self.uid, self.gid)
        item.dev = dev
        self.mongo.addFile(path,item.getMetadata())
        self.kv.addFile(path,item.getData())
        self.kv.commit()
        self._add_to_parent_dir(path)

    def create(self, path, flags, mode):
        logging.info("Creating File")
        item = Item(mode | stat.S_IFREG, self.uid, self.gid)
        self.mongo.updateFile(path,item.getMetadata())
        self.kv.updateFile(path,item.getData())
        self.kv.commit()
        self._add_to_parent_dir(path)

    def truncate(self, path, length):
        logging.info("Truncating file")
        data = self.kv.get(path)
        item = getItem(self.mongo.get(path))
        if data == None:
            return
        if item == None:
            return
        data=truncate(str(data),length)
        item.datalen=len(data)
        self.mongo.updateFile(path,item.getMetadata())
        self.kv.updateFile(path,data)
        self.kv.commit()


    def read(self, path, size, offset):
        logging.info("Reading File "+ path)
        logging.info("Size: %s Offset %s" % (size, offset))
        data = self.kv.get(path)
        return read(str(data), offset, size)

    def write(self, path, buf, offset):
        logging.info("Writing with buffer : %s offset: %s " % (buf, offset));
        logging.info("Write buffer is "+self.wbuff)
        returnBuf = len(buf)
        
        counter = 0
        while len(buf) > 0:
            n = self.PAGESIZE - self.HEADER - len(self.wbuff)
            
            self.wbuff += buf[:n]
            
            if (len(self.wbuff) == (self.PAGESIZE - self.HEADER)):
                #Get old data
                olddata = self.kv.get(path)
                
                #Append wbuffer to old data, encrypt, add header and return
                newdata, bytesWritten, totalLength = write(olddata, offset, self.wbuff)
                offset += bytesWritten
                #Flush to file system and commit
                self.kv.updateFile(path,newdata)
                self.kv.commit()
                counter = 1
                self.wbuff = ""
                #update metadata
                item = getItem(self.mongo.get(path))
                item.datalen=totalLength
                self.mongo.updateFile(path,item.getMetadata())
                
                #Add Log entry
                logging.info(str(bytesWritten)+" bytes written to "+path)
                
            buf = buf[n:]
        
        if counter == 0:
            olddata = self.kv.get(path)
            #Append wbuffer to old data, encrypt, add header and return
            newdata, bytesWritten, totalLength = write(olddata, offset, self.wbuff)
                
            #Flush to file system and commit
            self.kv.updateFile(path,newdata)
            self.kv.commit()
            counter = 1
            self.wbuff = ""
            #update metadata
            item = getItem(self.mongo.get(path))
            item.datalen=totalLength
            self.mongo.updateFile(path,item.getMetadata())
                
            #Add Log entry
            logging.info(str(bytesWritten)+" bytes written to "+path)
        
        return returnBuf

    # --- Directories --------------------------------------------------------
    def mkdir(self, path, mode):
        logging.info("Creating directory at path "+path)
        item = Item(mode | stat.S_IFDIR, self.uid, self.gid)
        self.mongo.addFile(path,item.getMetadata())
        self.kv.addFile(path,item.getData())
        self.kv.commit()
        self._add_to_parent_dir(path)

    def rmdir(self, path):
        logging.info("Deleting directory")
        data = self.kv.get(path)
        if data or data=='[]':
            return -errno.ENOTEMPTY
        self.mongo.deleteFile(path)
        self.kv.deleteFile(path)
        self.kv.commit()

    def readdir(self, path, offpathset):
        logging.info("Reading Directory")
        data = self.kv.get(path)
        dir_items=eval(data)
        for item in dir_items:
            yield fuse.Direntry(str(item))

    def _add_to_parent_dir(self, path):
        logging.info("Adding dir path = "+path)
        parent_path = os.path.dirname(path)
        filename = os.path.basename(path)
        data = self.kv.get(parent_path)
        logging.info("Parent data before adding"+data)
        dir_items=eval(data)
        if filename not in dir_items:
            logging.info(dir_items)
            dir_items.append(filename)
            logging.info(dir_items)
            self.kv.updateFile(parent_path,str(dir_items))
            self.kv.commit()
            item = getItem(self.mongo.get(parent_path))
            item.datalen =len(str(dir_items))
            self.mongo.updateFile(parent_path,item.getMetadata())
 
    def _remove_from_parent_dir(self, path):
        logging.info("Removing from parent directory")
        parent_path = os.path.dirname(path)
        filename = os.path.basename(path)
        data = self.kv.get(parent_path)
        logging.info("Parent directory data is "+data)
        dir_items=eval(data)

        if filename in dir_items:
            dir_items.remove(filename)
            logging.info(dir_items)
            self.kv.updateFile(parent_path,str(dir_items))
            self.kv.commit()
            item = getItem(self.mongo.get(parent_path))
            item.datalen=len(str(dir_items))
            self.mongo.updateFile(parent_path,item.getMetadata())
            logging.info("done updating")



