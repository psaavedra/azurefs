#!/usr/bin/env python
"""
A FUSE wrapper for locally mounting Azure blob storage

Ahmet Alp Balkan <ahmetalpbalkan at gmail.com>
Pablo Saavedra <saavedra.pablo@gmail.com>

Assumes AzureFS API 2011-08-18 at least (x-ms-range)

https://msdn.microsoft.com/library/azure/ee691967.aspx

"""

import time
import logging
import grp
import pwd

from sys import maxint
from stat import S_IFDIR, S_IFREG
from errno import *
from os import getuid
from fuse import FUSE, FuseOSError, Operations, LoggingMixIn
from azure.storage.blob import BlobService
from azure.common import AzureException
from azure.common import AzureMissingResourceHttpError
from requests.exceptions import ConnectionError

from multiprocessing import Process, Manager


TIME_FORMAT = '%a, %d %b %Y %H:%M:%S %Z'

if not hasattr(__builtins__, 'bytes'):
    bytes = str


def convert_to_epoch(date):
        """Converts Tue, 31 Jul 2012 07:17:34 GMT format to epoch"""
        return int(time.mktime(time.strptime(date, TIME_FORMAT)))


def get_files_from_blob_service(blobs, cname, files):
    log.info("Getting files from AzureFS: %s" % cname)
    marker = None
    available_attempts = 5
    while True:
        try:
            batch = blobs.list_blobs(cname, marker=marker)
            log.info("Populating %s: %s" % (cname, len(batch)))

            for b in batch:
                blob_name = b.name
                blob_date = b.properties.last_modified
                blob_size = long(b.properties.content_length)
                node = dict(st_mode=(S_IFREG | 0644), st_size=blob_size,
                            st_mtime=convert_to_epoch(blob_date),
                            st_uid=getuid())
                if blob_name.find('/') == -1:  # file just under container
                    files[blob_name] = node
            if not batch.next_marker:
                break
            marker = batch.next_marker
        except Exception as e:
            log.error("Remote connection error: %s" % e)
            available_attempts -= 1
            if available_attempts is 0:
                log.warning("No more attempts to connect to Azure. Ending the thread")
                break
            else:
                sleep_time = 5
                log.warning("Trying remote connection again in %s" % sleep_time)
                time.sleep(sleep_time)


class AzureFS(LoggingMixIn, Operations):
    """Azure Blob Storage filesystem"""

    blobs = None
    containers = dict()  # <cname, dict(stat:dict,
                                    #files:None|dict<fname, stat>)
    fd = 0

    def __init__(self, account, key):
        self.blobs = BlobService(account, key)
        self.rebuild_container_list()

    def rebuild_container_list(self):
        cmap = dict()
        cnames = set()
        for c in self.blobs.list_containers():
            date = c.properties.last_modified
            cstat = dict(st_mode=(S_IFDIR | 0755), st_uid=getuid(), st_size=0,
                         st_mtime=convert_to_epoch(date))
            cname = c.name
            cmap['/' + cname] = dict(stat=cstat, files=None)
            cnames.add(cname)

        cmap['/'] = dict(files={},
                         stat=dict(st_mode=(S_IFDIR | 0755),
                                   st_uid=getuid(), st_size=0,
                                   st_mtime=int(time.time())))

        self.containers = cmap   # destroys fs tree cache resistant to misses

    def _parse_path(self, path):    # returns </dir, file(=None)>
        if path.count('/') > 1:     # file
            return str(path[:path.rfind('/')]), str(path[path.rfind('/') + 1:])
        else:                       # dir
            pos = path.rfind('/', 1)
            if pos == -1:
                return path, None
            else:
                return str(path[:pos]), None

    def parse_container(self, path):
        base_container = path[1:]   # /abc/def/g --> abc
        if base_container.find('/') > -1:
            base_container = base_container[:base_container.find('/')]
        return str(base_container)

    def _get_dir(self, path, contents_required=False, force=False):
        cname = self.parse_container(path)

        if 'process' in self.containers['/' + cname] and \
                self.containers['/' + cname]['process'] is not None:
            p = self.containers['/' + cname]['process']
            if not p.is_alive():
                p.join()
                self.containers['/' + cname]['process'] = None

        if not self.containers:
            self.rebuild_container_list()
        if path in self.containers and not contents_required:
            return self.containers[path]

        if path in self.containers \
                and not force \
                and self.containers[path]['files'] is not None:
            return self.containers[path]

        if '/' + cname not in self.containers:
            raise FuseOSError(ENOENT)
        else:
            try:
                log.info(">>>> %s - %s " % (cname, self.containers['/' + cname]['process']))
            except Exception:
                log.info(">>>> no process: %s " % cname)
            if self.containers['/' + cname]['files'] is None or force is True:
                # fetch contents of container
                log.info("Contents not found in the cache index: %s" % cname)

                if 'process' in self.containers['/' + cname] and \
                        self.containers['/' + cname]['process'] is not None:
                    pass  # We do nothing. Some thread is still working
                    # getting files flob blobs
                    log.info("Launching of a new thread for recovery content for /%s skipped" % cname)
                else:  # no thread running for this container, so launching a new one
                    m = Manager()
                    files = m.dict()
                    p = Process(target=get_files_from_blob_service,
                                args=(self.blobs, cname, files, ))
                    p.daemon = True  # The process's daemon flag, a Boolean value. This must be
                    # set before start() is called.
                    # The initial value is inherited from the creating
                    # process.
                    # When a process exits, it attempts to terminate all of
                    # its daemonic child processes.
                    p.start()
                    self.containers['/' + cname]['process'] = p
                    log.info("> S >>> %s - %s " % (cname, self.containers['/' + cname]['process']))
                    self.containers['/' + cname]['files'] = files
            return self.containers['/' + cname]
        return None

    def _get_file(self, path):
        d, f = self._parse_path(path)
        log.debug("get_file: requested path=%s (d=%s, f=%s)", path, d, f)
        dir = self._get_dir(d, True)
        files = None
        if dir is not None:
            files = dir['files']
            if f in files:
                return files[f]

        if not hasattr(self, "_get_file_noent"):
            self._get_file_noent = {}

        last_check = self._get_file_noent.get(path, 0)
        if time.time() - last_check <= 30:   # Negative TTL is 30 seconds (hardcoded for now)
            log.info("get_file: cache says to reply negative for %s", path)
            return None

        # Check if file now exists and our caches are just stale.
        try:
            c = self.parse_container(d)
            p = path[path.find('/', 1) + 1:]
            props = self.blobs.get_blob_properties(c, p)
            log.info("get_debug: found localy unknown remote file %s: %s", path, repr(props))

            node = dict(st_mode=(S_IFREG | 0644), st_size=long(props.get('content-length', 0)),
                        st_mtime=convert_to_epoch(props['last-modified']),
                        st_uid=getuid())

            if node['st_size'] > 0:
                log.info("get_debug: properties for %s: %s", path, repr(node))
                files[f] = node  # Remember this, so we won't have to re-query it.
                if path in self._get_file_noent:
                    del self._get_file_noent[path]
                return node
            else:
                # Sometimes the file is not yet here and is still uploading.
                # Such files have "content-length: 0". Let's ignore those for now.
                log.warning("get_file: the file %s is not yet here (size=%s)", path, node['st_size'])
                self._get_file_noent[path] = time.time()
                return None
        except AzureMissingResourceHttpError:
            log.info("get_file: remote confirms non-existence of %s", path)
            self._get_file_noent[path] = time.time()
            return None
        except AzureException as e:
            log.error("get_file: exception while querying remote for %s: %s", path, repr(e))
            self._get_file_noent[path] = time.time()
        except ConnectionError as e:
            log.error("get_file: exception while querying remote for %s: %s", path, repr(e))
            self._get_file_noent[path] = time.time()
            return None

        return None

    def getattr(self, path, fh=None):
        d, f = self._parse_path(path)

        if f is None:
            dir = self._get_dir(d)
            return dir['stat']
        else:
            file = self._get_file(path)

            if file:
                return file

        raise FuseOSError(ENOENT)

    # FUSE
    def mkdir(self, path, mode):
        if path.count('/') <= 1:    # create on root
            name = path[1:]

            if not 3 <= len(name) <= 63:
                log.error("Container names can be 3 through 63 chars long.")
                raise FuseOSError(ENAMETOOLONG)
            if name is not name.lower():
                log.error("Container names cannot contain uppercase \
                        characters.")
                raise FuseOSError(EACCES)
            if name.count('--') > 0:
                log.error('Container names cannot contain consecutive \
                        dashes (-).')
                raise FuseOSError(EAGAIN)
            #TODO handle all "-"s must be preceded by letter or numbers
            #TODO starts with only letter or number, can contain letter, nr,'-'

            resp = self.blobs.create_container(name)

            if resp:
                self.rebuild_container_list()
                log.info("CONTAINER %s CREATED" % name)
            else:
                raise FuseOSError(EACCES)
                log.error("Invalid container name or container already \
                        exists.")
        else:
            raise FuseOSError(ENOSYS)  # TODO support 2nd+ level mkdirs

    def rmdir(self, path):
        if path.count('/') == 1:
            c_name = path[1:]
            resp = self.blobs.delete_container(c_name)

            if resp:
                if path in self.containers:
                    del self.containers[path]
            else:
                raise FuseOSError(EACCES)
        else:
            raise FuseOSError(ENOSYS)  # TODO support 2nd+ level mkdirs

    def create(self, path, mode):
        node = dict(st_mode=(S_IFREG | mode), st_size=0, st_nlink=1,
                    st_uid=getuid(), st_mtime=time.time())
        d, f = self._parse_path(path)

        if not f:
            log.error("Cannot create files on root level: /")
            raise FuseOSError(ENOSYS)

        if f == ".__refresh_cache__":
            log.info("Refresh cache forced (%s)" % f)
            self._get_dir(path, True, True)
            return self.open(path, data='')

        dir = self._get_dir(d, True)
        if not dir:
            raise FuseOSError(EIO)
        dir['files'][f] = node

        return self.open(path, data='')     # reusing handler provider

    def open(self, path, flags=0, data=None):
        if data is None:                    # download contents
            c_name = self.parse_container(path)
            f_name = path[path.find('/', 1) + 1:]

            try:
                self.blobs.get_blob_metadata(c_name, f_name)
            except AzureMissingResourceHttpError:
                dir = self._get_dir('/' + c_name, True)
                if f_name in dir['files']:
                    del dir['files'][f_name]
                raise FuseOSError(ENOENT)
            except AzureException as e:
                log.error("Read blob failed HTTP %d" % e.code)
                raise FuseOSError(EAGAIN)
        self.fd += 1
        return self.fd

    # def flush(self, path, fh=None):
    #     # TODO: Re-conctruct a local memory cache in the next future

    # def release(self, path, fh=None):
    #     # TODO: Re-conctruct a local memory cache in the next future

    def truncate(self, path, length, fh=None):
        return 0     # assume done, no need

    def write(self, path, data, offset, fh=None):
        # TODO: Re-conctruct a local memory cache in the next future
        # f_name = path[path.find('/', 1) + 1:]
        # c_name = path[1:path.find('/', 1)]
        # file_type = mimetypes.guess_type(f_name)
        # size = len(data)
        raise FUSEOSError(EPERM)

    def unlink(self, path):
        c_name = self.parse_container(path)
        d, f = self._parse_path(path)

        try:
            self.blobs.delete_blob(c_name, f)

            _dir = self._get_dir(path, True)
            if _dir and f in _dir['files']:
                del _dir['files'][f]
            return 0
        except AzureMissingResourceHttpError:
            raise FuseOSError(ENOENT)
        except Exception:
            raise FuseOSError(EAGAIN)

    def readdir(self, path, fh):
        if path == '/':
            return ['.', '..'] + [x[1:] for x in self.containers.keys() if x is not '/']

        dir = self._get_dir(path, True)
        if not dir:
            raise FuseOSError(ENOENT)
        return ['.', '..'] + dir['files'].keys()

    def read(self, path, size, offset, fh):
        f_name = path[path.find('/', 1) + 1:]
        c_name = path[1:path.find('/', 1)]

        try:
            range_ = "bytes=%s-%s" % (offset, offset + size - 1)
            log.debug("read range: %s" % range_)
            data = self.blobs.get_blob(c_name, f_name, snapshot=None,
                                       x_ms_range=range_)
            return data
        except TypeError, e:
            if e.code == 404:
                raise FuseOSError(ENOENT)
            elif e.code == 403:
                raise FUSEOSError(EPERM)
            else:
                log.error("Read blob failed HTTP %d" % e.code)
                raise FuseOSError(EAGAIN)

    def statfs(self, path):
        return dict(f_bsize=4096, f_blocks=1, f_bavail=maxint)

    def rename(self, old, new):
        """Three stage move operation because Azure do not have
        move or rename call. """
        # TODO: Implement rename operation in the next future
        raise FuseOSError(ENOSYS)

    def symlink(self, target, source):
        raise FuseOSError(ENOSYS)

    def getxattr(self, path, name, position=0):
        return ''

    def chmod(self, path, mode):
        pass

    def chown(self, path, uid, gid):
        pass

import argparse


if __name__ == '__main__':
    log = logging.getLogger()
    ch = logging.StreamHandler()
    log.addHandler(ch)


description = ''
parser = argparse.ArgumentParser(description=description)
parser.add_argument('account', metavar='AzureAccount',
                    help='Azure account')
parser.add_argument('secretkey', metavar='AzureSecretKey',
                    help='Azure sercret key')
parser.add_argument('mountpoint', metavar='LocalPath',
                    help='the local mount point')

parser.add_argument('--mkdir', action='store_true',
                    help='create mountpoint if not found (and create intermediate directories as required)')
parser.add_argument('--nonempty', action='store_true',
                    help='allows mounts over a non-empty file or directory')
parser.add_argument('--uid', metavar='N',
                    help='default UID')
parser.add_argument('--gid', metavar='N',
                    help='default GID')
parser.add_argument('--umask', metavar='MASK',
                    help='default umask')
parser.add_argument('--read-only', action='store_true',
                    help='mount read only')

parser.add_argument('--no-allow-other', action='store_true',
                    help='Do not allow other users to access mounted directory')

parser.add_argument('-f', '--foreground', action='store_true',
                    help='run in foreground')
parser.add_argument('-d', '--debug', action='store_true',
                    help='show debug info')

options = parser.parse_args()

if options.debug:
    log.setLevel(logging.DEBUG)
else:
    log.setLevel(logging.INFO)
log.debug("options = %s" % options)

if options.mkdir:
    create_dirs(options.mountpoint)

mount_options = {
    'mountpoint': options.mountpoint,
    'fsname': 'azurefs',
    'foreground': options.foreground,
    'allow_other': True,
    'auto_cache': True,
    'atime': False,
    'max_read': 131072,
    'max_write': 131072,
    'max_readahead': 131072,
}

if options.no_allow_other:
    mount_options["allow_other"] = False
if options.uid:
    try:
        mount_options['uid'] = pwd.getpwnam(options.uid).pw_uid
    except Exception, e:
        mount_options['uid'] = options.uid
if options.gid:
    try:
        mount_options['gid'] = grp.getgrnam(options.gid).gr_gid
    except:
        mount_options['gid'] = options.gid
if options.umask:
    mount_options['umask'] = options.umask
if options.read_only:
    mount_options['ro'] = True

if options.nonempty:
    mount_options['nonempty'] = True

mount_options['nothreads'] = False

if __name__ == '__main__':
    log.debug("mount options: %s" % mount_options)
    fuse = FUSE(AzureFS(options.account, options.secretkey), **mount_options)
