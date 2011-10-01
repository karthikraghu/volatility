# Volatility
# Copyright (c) 2008 Volatile Systems
# Copyright (c) 2008 Brendan Dolan-Gavitt <bdolangavitt@wesleyan.edu>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details. 
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA 
#

import datetime
import socket, struct
import volatility.plugins.overlays.basic as basic
import volatility.plugins.kpcrscan as kpcr
import volatility.plugins.kdbgscan as kdbg
import volatility.timefmt as timefmt
import volatility.debug as debug
import volatility.obj as obj
import volatility.addrspace as addrspace

class AbstractWindowsx86(obj.Profile):
    """ A Profile for Windows systems """
    _md_os = 'windows'
    _md_memory_model = '32bit'
    native_types = basic.x86_native_types_32bit

AbstractWindows = AbstractWindowsx86

class AbstractWindowsx64(obj.Profile):
    """ A Profile for Windows systems """
    _md_os = 'windows'
    _md_memory_model = '64bit'
    native_types = basic.x86_native_types_64bit

class _UNICODE_STRING(obj.CType):
    """Class representing a _UNICODE_STRING

    Adds the following behavior:
      * The Buffer attribute is presented as a Python string rather
        than a pointer to an unsigned short.
      * The __str__ method returns the value of the Buffer.
    """
    def v(self, vm = None):
        try:
            length = self.Length.v()
            if length > 1024:
                length = 0
            data = (vm or self.obj_vm).read(self.Buffer.v(), length)
            return data.decode("utf16", "ignore").encode("ascii", 'backslashreplace')
        except Exception, _e:
            return ''

    def __nonzero__(self):
        ## Unicode strings are valid if they point at a valid memory
        return bool(self.Buffer)

    def __format__(self, formatspec):
        return format(self.v(), formatspec)

    def __str__(self):
        return self.v()

AbstractWindows.object_classes['_UNICODE_STRING'] = _UNICODE_STRING

class _LIST_ENTRY(obj.CType):
    """ Adds iterators for _LIST_ENTRY types """
    def list_of_type(self, type, member, forward = True):
        if not self.is_valid():
            return

        ## Get the first element
        if forward:
            lst = self.Flink.dereference()
        else:
            lst = self.Blink.dereference()

        offset = self.obj_vm.profile.get_obj_offset(type, member)

        seen = set()
        seen.add(lst.obj_offset)

        while 1:
            ## Instantiate the object
            item = obj.Object(type, offset = lst.obj_offset - offset,
                                    vm = self.obj_vm,
                                    parent = self.obj_parent,
                                    name = type)


            if forward:
                lst = item.m(member).Flink.dereference()
            else:
                lst = item.m(member).Blink.dereference()

            if not lst.is_valid() or lst.obj_offset in seen:
                return
            seen.add(lst.obj_offset)

            yield item

    def __nonzero__(self):
        ## List entries are valid when both Flinks and Blink are valid
        return bool(self.Flink) or bool(self.Blink)

    def __iter__(self):
        return self.list_of_type(self.obj_parent.obj_name, self.obj_name)

AbstractWindows.object_classes['_LIST_ENTRY'] = _LIST_ENTRY

class WinTimeStamp(obj.NativeType):
    """Class for handling Windows Time Stamps"""

    def __init__(self, theType = None, offset = None, vm = None,
                 parent = None, name = None, is_utc = False, **args):
        self.is_utc = is_utc
        obj.NativeType.__init__(self, theType, offset, vm, parent = parent,
                                name = name, format_string = "q")

    def windows_to_unix_time(self, windows_time):
        """
        Converts Windows 64-bit time to UNIX time

        @type  windows_time:  Integer
        @param windows_time:  Windows time to convert (64-bit number)

        @rtype  Integer
        @return  UNIX time
        """
        if(windows_time == 0):
            unix_time = 0
        else:
            unix_time = windows_time / 10000000
            unix_time = unix_time - 11644473600

        if unix_time < 0:
            unix_time = 0

        return unix_time

    def as_windows_timestamp(self):
        return obj.NativeType.v(self)

    def v(self, vm = None):
        value = self.as_windows_timestamp()
        return self.windows_to_unix_time(value)

    def __nonzero__(self):
        return self.v() != 0

    def __str__(self):
        return "{0}".format(self)

    def as_datetime(self):
        try:
            dt = datetime.datetime.utcfromtimestamp(self.v())
            if self.is_utc:
                # Only do dt.replace when dealing with UTC
                dt = dt.replace(tzinfo = timefmt.UTC())
        except ValueError, e:
            return obj.NoneObject("Datetime conversion failure: " + str(e))
        return dt

    def __format__(self, formatspec):
        """Formats the datetime according to the timefmt module"""
        dt = self.as_datetime()
        if dt != None:
            return format(timefmt.display_datetime(dt), formatspec)
        return "-"

AbstractWindows.object_classes['WinTimeStamp'] = WinTimeStamp

class _EPROCESS(obj.CType):
    """ An extensive _EPROCESS with bells and whistles """
    def _Peb(self, _attr):
        """ Returns a _PEB object which is using the process address space.

        The PEB structure is referencing back into the process address
        space so we need to switch address spaces when we look at
        it. This method ensure this happens automatically.
        """
        process_ad = self.get_process_address_space()
        if process_ad:
            offset = self.m("Peb").v()
            peb = obj.Object("_PEB", offset, vm = process_ad,
                                    name = "Peb", parent = self)

            if peb.is_valid():
                return peb

        return obj.NoneObject("Peb not found")

    def get_process_address_space(self):
        """ Gets a process address space for a task given in _EPROCESS """
        directory_table_base = self.Pcb.DirectoryTableBase.v()

        try:
            process_as = self.obj_vm.__class__(self.obj_vm.base, self.obj_vm.get_config(), dtb = directory_table_base)
        except AssertionError, _e:
            return obj.NoneObject("Unable to get process AS")

        process_as.name = "Process {0}".format(self.UniqueProcessId)

        return process_as

AbstractWindows.object_classes['_EPROCESS'] = _EPROCESS

class _HANDLE_TABLE(obj.CType):
    """ A class for _HANDLE_TABLE. 
    
    This used to be a member of _EPROCESS but it was isolated per issue 
    91 so that it could be subclassed and used to service other handle 
    tables, such as the _KDDEBUGGER_DATA64.PspCidTable.
    """

    def get_item(self, offset):
        """Returns the OBJECT_HEADER of the associated handle at a particular offset"""
        return obj.Object("_OBJECT_HEADER", offset, self.obj_vm,
                                            parent = self)

    def _make_handle_array(self, offset, level):
        """ Returns an array of _HANDLE_TABLE_ENTRY rooted at offset,
        and iterates over them.

        """
        if level > 0:
            count = 0x400
            targetType = "unsigned int"
        else:
            count = 0x200
            targetType = "_HANDLE_TABLE_ENTRY"

        table = obj.Object("Array", offset = offset, vm = self.obj_vm, count = count,
                           targetType = targetType, parent = self)

        if table:
            for entry in table:
                if not entry.is_valid():
                    break

                if level > 0:
                    ## We need to go deeper:
                    for h in self._make_handle_array(entry, level - 1):
                        yield h
                else:
                    ## OK We got to the bottom table, we just resolve
                    ## objects here:
                    obj_offset = int(entry.Object.v()) & ~0x00000007

                    item = self.get_item(obj_offset)

                    if item == None:
                        continue

                    try:
                        # New object header
                        if item.TypeIndex != 0x0:
                            yield item
                    except AttributeError:
                        if item.Type.Name:
                            yield item

    def handles(self):
        """ A generator which yields this process's handles

        _HANDLE_TABLE tables are multi-level tables at the first level
        they are pointers to second level table, which might be
        pointers to third level tables etc, until the final table
        contains the real _OBJECT_HEADER table.

        This generator iterates over all the handles recursively
        yielding all handles. We take care of recursing into the
        nested tables automatically.
        """
        # This should work equally for 32 and 64 bit systems
        LEVEL_MASK = 7

        TableCode = self.TableCode.v() & ~LEVEL_MASK
        table_levels = self.TableCode.v() & LEVEL_MASK
        offset = TableCode
        count = self.HandleCount.v()

        for h in self._make_handle_array(offset, table_levels):
            yield h

            count -= 1
            if count == 0:
                break


AbstractWindows.object_classes['_HANDLE_TABLE'] = _HANDLE_TABLE

class _OBJECT_HEADER(obj.CType):
    """A Volatility object to handle Windows object headers.

    This object applies only to versions below windows 7.
    """

    def __init__(self, *args, **kwargs):
        # kernel AS for dereferencing pointers 
        self.kas = None
        obj.CType.__init__(self, *args, **kwargs)

        # Create accessors for optional headers
        self.find_optional_headers()

    optional_headers = (
        ('_OBJECT_HEADER_NAME_INFO', 'NameInfoOffset'),
        #('_OBJECT_HEADER_HANDLE_INFO', 'HandleInfoOffset'),
        ('_OBJECT_HEADER_QUOTA_INFO', 'QuotaInfoOffset'),
        )

    def find_optional_headers(self):
        """Find this object's optional headers."""
        offset = self.obj_offset

        for name, info_offset in self.optional_headers:
            header_offset = self.m(info_offset).v()
            if header_offset:
                o = obj.Object(name, offset-header_offset, self.obj_vm)
            else:
                o = obj.NoneObject("Header not set")

            self.newattr(name, o)

    def get_object_type(self):
        """Return the object's type as a string"""
        type_obj = obj.Object("_OBJECT_TYPE", self.Type, self.kas)

        return type_obj.Name.v()


AbstractWindows.object_classes['_OBJECT_HEADER'] = _OBJECT_HEADER

## This is an object which provides access to the VAD tree.
class _MMVAD(obj.CType):
    """Class factory for _MMVAD objects"""

    ## The actual type depends on this tag value.
    tag_map = {'Vadl': '_MMVAD_LONG',
               'VadS': '_MMVAD_SHORT',
               'Vad ': '_MMVAD_LONG',
               'VadF': '_MMVAD_SHORT',
               'Vadm': '_MMVAD_LONG',
              }

    ## parent is the containing _EPROCESS right now
    def __new__(cls, theType, offset, vm, parent, **args):
        ## All VADs are done in the process AS - so we might need to switch
        ## Address spaces now. Find the eprocess we came from and switch
        ## AS. Note that all child traversals will be in Process AS. 
        if vm.name.startswith("Kernel"):
            # Find the next _EPROCESS along our parent list
            eprocess = parent
            while eprocess and eprocess.obj_name != "_EPROCESS":
                eprocess = eprocess.obj_parent

            # Switch to its process AS
            vm = eprocess.get_process_address_space()

        # Start off with an _MMVAD_LONG
        result = obj.Object('_MMVAD_LONG', offset = offset, vm = vm,
                            parent = parent, **args)

        # Get the tag and change the vad type if necessary
        real_type = cls.tag_map.get(result.Tag, '_MMVAD_LONG')
        if result.__class__.__name__ != real_type:
            result = obj.Object(real_type, offset = offset, vm = vm,
                                parent = parent, **args)

        return result

AbstractWindows.object_classes['_MMVAD'] = _MMVAD

class _MMVAD_SHORT(obj.CType):
    """Class with convenience functions for _MMVAD_SHORT functions"""
    def traverse(self, visited = None):
        """ Traverse the VAD tree by generating all the left items,
        then the right items.

        We try to be tolerant of cycles by storing all offsets visited.
        """
        if visited == None:
            visited = set()

        ## We try to prevent loops here
        if self.obj_offset in visited:
            return

        yield self

        for c in self.LeftChild.traverse(visited = visited):
            visited.add(c.obj_offset)
            yield c

        for c in self.RightChild.traverse(visited = visited):
            visited.add(c.obj_offset)
            yield c

    def get_parent(self):
        """Returns the Parent of the MMVAD"""
        return self.Parent

    def get_control_area(self):
        """Returns the ControlArea of the MMVAD"""
        return self.ControlArea

    def get_file_object(self):
        """Returns the FilePointer of the ControlArea of the MMVAD"""
        return self.ControlArea.FilePointer

    def get_start(self):
        """Get the starting virtual address"""
        return self.StartingVpn << 12

    def get_end(self):
        """Get the ending virtual address"""
        return ((self.EndingVpn + 1) << 12) - 1

    def get_data(self):
        """Get the data in a vad region"""

        start = self.get_start()
        end = self.get_end()

        num_pages = (end - start + 1) >> 12

        blank_page = '\x00' * 0x1000
        pages_list = [(self.obj_vm.read(start + index * 0x1000, 0x1000) if self.obj_vm.is_valid_address(start + index * 0x1000) else blank_page) for index in xrange(num_pages)]
        if None in pages_list:
            pages_list = [a_page if a_page != None else blank_page for a_page in pages_list]
        return ''.join(pages_list)

class _MMVAD_LONG(_MMVAD_SHORT):
    """Subclasses _MMVAD_LONG based on _MMVAD_SHORT"""
    pass

AbstractWindows.object_classes['_MMVAD_SHORT'] = _MMVAD_SHORT
AbstractWindows.object_classes['_MMVAD_LONG'] = _MMVAD_LONG

class _EX_FAST_REF(obj.CType):
    """Overrides the dereferencing of EX_FAST_REF objects"""

    def dereference_as(self, theType, addr_space = None):
        """Use the _EX_FAST_REF.Object pointer to resolve an object of the specified type"""
        return obj.Object(theType, vm = addr_space or self.obj_vm, parent = self, offset = self.Object.v() & ~7)

AbstractWindows.object_classes['_EX_FAST_REF'] = _EX_FAST_REF

class ThreadCreateTimeStamp(WinTimeStamp):
    """Handles ThreadCreateTimeStamps which are bit shifted WinTimeStamps"""
    def __init__(self, *args, **kwargs):
        WinTimeStamp.__init__(self, *args, **kwargs)

    def as_windows_timestamp(self):
        return obj.NativeType.v(self) >> 3

AbstractWindows.object_classes['ThreadCreateTimeStamp'] = ThreadCreateTimeStamp

class _TCPT_OBJECT(obj.CType):
    """Provides additional functions for TCPT_OBJECTs"""

    def _RemoteIpAddress(self, attr):
        """Returns the remote IP address of the TCPT_OBJECT"""
        return socket.inet_ntoa(struct.pack("<I", self.m(attr).v()))

    def _LocalIpAddress(self, attr):
        """Returns the local IP address of the TCPT_OBJECT"""
        return socket.inet_ntoa(struct.pack("<I", self.m(attr).v()))

AbstractWindows.object_classes['_TCPT_OBJECT'] = _TCPT_OBJECT

class _ADDRESS_OBJECT(obj.CType):
    """Provides additional functions for ADDRESS_OBJECTs"""

    def _LocalIpAddress(self, attr):
        """Returns the local IP address of the ADDRESS_OBJECT"""
        return socket.inet_ntoa(struct.pack("<I", self.m(attr).v()))

AbstractWindows.object_classes['_ADDRESS_OBJECT'] = _ADDRESS_OBJECT

class VolatilityKPCR(obj.VolatilityMagic):
    """A scanner for KPCR data within an address space"""

    def generate_suggestions(self):
        """Returns the results of KCPRScanner for an adderss space"""
        scanner = kpcr.KPCRScanner()
        for val in scanner.scan(self.obj_vm):
            yield val

AbstractWindows.object_classes['VolatilityKPCR'] = VolatilityKPCR

class VolatilityKDBG(obj.VolatilityMagic):
    """A Scanner for KDBG data within an address space"""

    def generate_suggestions(self):
        """Generates a list of possible KDBG structure locations"""
        scanner = kdbg.KDBGScanner(needles = [obj.VolMagic(self.obj_vm).KDBGHeader.v()])
        for val in scanner.scan(self.obj_vm):
            yield val

AbstractWindows.object_classes['VolatilityKDBG'] = VolatilityKDBG

class VolatilityIA32ValidAS(obj.VolatilityMagic):
    """An object to check that an address space is a valid IA32 Paged space"""

    def generate_suggestions(self):
        """Generates a single response of True or False depending on whether the space is a valid Windows AS"""
        # This constraint looks for self referential values within
        # the paging tables
        try:
            if self.obj_vm.pae:
                pde_base = 0xc0600000
                pd = self.obj_vm.get_pdpte(0) & 0xffffffffff000
            else:
                pde_base = 0xc0300000
                pd = self.obj_vm.dtb
            if (self.obj_vm.vtop(pde_base) == pd):
                yield True
                raise StopIteration

        except addrspace.ASAssertionError, _e:
            pass
        debug.debug("Failed to pass the Moyix Valid IA32 AS test", 3)

        # This constraint verifies that _KUSER_ SHARED_DATA is shared
        # between user and kernel address spaces.
        if (self.obj_vm.vtop(0xffdf0000)) == (self.obj_vm.vtop(0x7ffe0000)):
            if self.obj_vm.vtop(0xffdf0000) != None:
                yield True
                raise StopIteration
        debug.debug("Failed to pass the labarum_x Valid IA32 AS test", 3)

        yield False

AbstractWindows.object_classes['VolatilityIA32ValidAS'] = VolatilityIA32ValidAS
