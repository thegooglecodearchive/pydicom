# filereader.py
"""Read a dicom media file"""
#
# Copyright 2004, 2008, Darcy Mason
# This file is part of pydicom.
#
# pydicom is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# pydicom is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License (license.txt) for more details 

from struct import unpack, calcsize, pack
# Need zlib and cStringIO for deflate-compressed file
import zlib
from StringIO import StringIO # tried cStringIO but wouldn't let me derive class from it.
import logging
logger = logging.getLogger('pydicom')

from dicom.UID import UID, UID_dictionary
from dicom.UID import DeflatedExplicitVRLittleEndian, ExplicitVRLittleEndian
from dicom.UID import ImplicitVRLittleEndian, ExplicitVRBigEndian
from dicom.filebase import DicomFile, DicomStringIO
from dicom.datadict import dictionaryVR, dictionaryDescription
from dicom.dataset import Dataset
from dicom.dataelem import DataElement
from dicom.tag import Tag, ItemTag, ItemDelimiterTag, SequenceDelimiterTag
from dicom.sequence import Sequence
from dicom.valuerep import PersonName, MultiValue, MultiString

from sys import byteorder
sys_isLittleEndian = (byteorder == 'little')

def read_VR(fp):
    """Return the two character Value Representation string"""
    return unpack("2s", fp.read(2))[0]

def read_numbers(fp, length, format):
    """Read a "value" of type format from the dicom file. "Value" can be more than one number"""
    endianChar = '><'[fp.isLittleEndian]
    bytes_per_value = calcsize("="+format) # "=" means use 'standard' size, needed on 64-bit systems.
    data = fp.read(length)

    format_string = "%c%u%c" % (endianChar, length/bytes_per_value, format) 
    value = unpack(format_string, data)
    if len(value) == 1:
        return value[0]
    else:        
        return list(value)  # convert from tuple to a list so can modify if need to

def read_OBvalue(fp, length):
    """Return the raw bytes from reading an OB value
    
    if length is greater than """
    isUndefinedLength = False
    if length == 0xffffffffL: # undefined length. PS3.6-2008 Tbl 7.1-1, then read to Sequence Delimiter Item
        isUndefinedLength = True
        length = length_of_undefined_length(fp, SequenceDelimiterTag)
    data = fp.read(length)
    # logger.debug("len(data): %d; length=%d", len(data), length)
    # logger.debug("OB before absorb: 0x%x", fp.tell())
    if isUndefinedLength:
        absorb_delimiter_item(fp, Tag(SequenceDelimiterTag))
    return data

def read_OWvalue(fp, length):
    """Return the raw bytes from reading an OW value rep
    
    Note: pydicom does not do byte swapping if necessary, except in 
    dataset.PixelArray function
    """
    return read_OBvalue(fp, length) # for now, Maybe later will have own routine

def read_UI(fp, length):
    value = fp.read(length)
    # Strip off 0-byte padding for even length (if there)
    if value and value.endswith('\0'):
        value = value[:-1]
    return MultiString(value, UID)

def read_string(fp, length):
    return MultiString(fp.read(length))
    
def read_PN(fp, length):
    return MultiString(fp.read(length), valtype=PersonName)

def read_single_string(fp, length):
    """Read a single string; the backslash used to separate values in multi-strings
    has no meaning here"""
    val = fp.read(length)
    if val and val.endswith(' '):
        val = val[:-1]
    return val

def read_data_element(fp, length=None):
    data_element_tell = fp.tell()
    try:
        tag = fp.read_tag()
    except EOFError:
        return None
    
    if tag==ItemDelimiterTag or tag==SequenceDelimiterTag:
        length = fp.read_UL()
        if length != 0:
            logger.warning("Expected 0x00000000 after delimiter, found 0x%x, at position 0x%x", length, fp.tell()-4)
        data_element = DataElement(tag, None, None, data_element_tell)
        logger.debug("%04x: %s", data_element_tell, str(data_element))
        return data_element
        
    # Get the value representation VR
    if fp.isImplicitVR:
        try:
            VR = dictionaryVR(tag)
        except KeyError:
            if tag.isPrivate:
                VR = 'OB'  # just read the bytes, no way to know what they mean
            elif tag.element == 0:  # group length tag implied in versions < 3.0
                VR = 'UL'
            else:
                raise KeyError, "Unknown DICOM tag %s - can't look up VR" % str(tag)
    else:
        VR = read_VR(fp)
    if VR not in readers:
        raise NotImplementedError, "Unknown Value Representation '%s' in tag %s" % (VR, tag)

    # Get the length field of the data element
    if fp.isExplicitVR:
        if VR in ['OB','OW','SQ','UN', 'UT']:
            reserved = fp.read(2)
            length = fp.read_UL()
        else:
            length = fp.read_US()
    else: # Implicit VR
        length = fp.read_UL()

    isUndefinedLength = (length == 0xFFFFFFFFL)
    value_tell = fp.tell() # store file location and size, for programs like anonymizers
    length_original = length
    if VR == "SQ":
        temp_data_element = DataElement(tag,VR, Sequence(), data_element_tell)
        logger.debug("%04x: %s", data_element_tell, temp_data_element)
        logger.debug("                         -------> SQ is using %s", ["explicit length", "Undefined Length"][isUndefinedLength])
    try:
        readers[VR][0] # if reader is a tuple, then need to pass a number format
    except TypeError:
        value = readers[VR](fp, length) # call the function to read that kind of item
    else:
        value = readers[VR][0](fp, length, readers[VR][1])
    if tag == 0x00280103: # This flags whether pixel values are US (val=0) or SS (val = 1)
        fp.isSSpixelRep = value # XXX This is not used anywhere else in code?
    data_element = DataElement(tag, VR, value, value_tell)
    data_element.isUndefinedLength = isUndefinedLength # store this to write back data element in same way was read
    if data_element.VR != "SQ":
        logger.debug("%04x: %s", data_element_tell, str(data_element))
    return data_element

def read_dataset(fp, bytelength=None):
    """Return a Dataset dictionary containing DataElements starting from
    the current file position through the following bytelength bytes
    The dictionary key is the Dicom (group, element) tag, and the dictionary
    value is the DataElement class instance
    """
    ds = Dataset()
    fpStart = fp.tell()
    while (bytelength is None) or (fp.tell()-fpStart < bytelength):
        data_element = read_data_element(fp)
        if data_element is None: # None if end-of-file
            break        
        if data_element.tag == ItemDelimiterTag: # dataset is an item in a sequence
            break
        ds.Add(data_element)
    
    # Test that got the expected number of bytes if explicit length given
    bytes_read = fp.tell() - fpStart
    if bytelength is not None and bytes_read != bytelength:
        logging.warning("Expected dataset length %d bytes, got %d" % (bytelength, bytes_read))
    return ds


def read_sequence(fp, bytelength):
    """Return a Sequence list of Datasets"""
    seq = Sequence()
    if bytelength == 0:  # Sequence of length 0 is possible (PS 3.5-2008 7.5.1a (p.40)
        return seq
    seq.isUndefinedLength = False
    if bytelength == 0xffffffffL:
        seq.isUndefinedLength = True
        bytelength = None
    fpStart = fp.tell()            
    while (not bytelength) or (fp.tell()-fpStart < bytelength):
        dataset = read_sequence_item(fp)
        if dataset is None:  # None is returned if get to Sequence Delimiter
            break
        seq.append(dataset)
    return seq

def read_sequence_item(fp):
    tag = fp.read_tag()
    if tag == SequenceDelimiterTag: # No more items, time for sequence to stop reading
        data_element = DataElement(tag, None, None, fp.tell()-4)
        logger.debug("%04x: %s", fp.tell()-4, str(data_element))
        length = fp.read_UL()
        if length != 0:
            logger.warning("Expected 0x00000000 after delimiter, found 0x%x, at position 0x%x", length, fp.tell()-4)
        return None
    if tag != ItemTag:
        logger.warning("Expected sequence item with tag %s at file position 0x%x", (ItemTag, fp.tell()-4))
    else:
        logger.debug("%04x: Found Item tag (start of item)", fp.tell()-4)
    length = fp.read_UL()
    isUndefinedLength = False
    if length == 0xFFFFFFFFL:
        isUndefinedLength = True
        length = None # length_of_undefined_length(fp, ItemDelimiterTag)
    ds = read_dataset(fp, length)
    ds.isUndefinedLengthSequenceItem = isUndefinedLength
    # if isUndefinedLength:
        # absorb_delimiter_item(fp, ItemDelimiterTag)
    return ds

def absorb_delimiter_item(fp, delimiter):
    """Used to read (and ignore) undefined length sequence or item terminators."""
    tag = fp.read_tag()
    if tag != delimiter:
        logger.warn("Did not find expected delimiter '%s', instead found %s at file position 0x%x", dictionaryDescription(delimiter), str(tag), fp.tell()-4)    
        fp.seek(fp.tell()-4)
        return 
    logger.debug("%04x: Found Delimiter '%s'", fp.tell()-4, dictionaryDescription(delimiter))
    length = fp.read_UL() # 4 bytes for 'length', all 0's
    if length == 0:
        logger.debug("%04x: Read 0 bytes after delimiter", fp.tell()-4)
    else:
        logger.debug("%04x: Expected 0x00000000 after delimiter, found 0x%x", fp.tell()-4, length)

def find_bytes(fp, bytes_to_find, read_size=128):
    data_start = fp.tell()  
    rewind = len(bytes_to_find)-1
    
    found = False
    while not found:
        chunk_start = fp.tell()
        bytes = fp.read(read_size)
        if not bytes:
            fp.seek(data_start)
            return None
        index = bytes.find(bytes_to_find)
        if index != -1:
            found = True
        else:
            fp.seek(fp.tell()-rewind) # rewind a bit in case delimiter crossed read_size boundary
    # if get here then have found the byte string
    found_at = chunk_start + index
    fp.seek(data_start)
    return found_at

def find_delimiter(fp, delimiter, read_size=128):
    """Return file position where 4-byte delimiter is located.
    
    Return None if reach end of file without finding the delimiter.
    On return, file position will be where it was before this function.
    
    """
    format = "<H"
    if fp.isBigEndian:
        format = ">H"
    delimiter = Tag(delimiter)
    bytes_to_find = pack(format, delimiter.group) + pack(format, delimiter.elem)
    return find_bytes(fp, bytes_to_find)            
    
def length_of_undefined_length(fp, delimiter, read_size=128):
    """Search through the file to find the delimiter, return the length of the data
    element value that the dicom file writer was too lazy to figure out for us.
    Return the file to the start of the data, ready to read it.
    Note the data element that the delimiter starts is not read here, the calling
    routine must handle that.
    delimiter must be 4 bytes long"""
    chunk = 0
    data_start = fp.tell()
    delimiter_pos = find_delimiter(fp, delimiter)
    length = delimiter_pos - data_start
    return length
    
def read_delimiter_item(fp, delimiter):
    found = fp.read(4)
    if found != delimiter:
        logger.warn("Expected delimitor %s, got %s at file position 0x%x", Tag(delimiter), Tag(found), fp.tell()-4)
    length = fp.read_UL()
    if length != 0:
        logger.warn("Expected delimiter item to have length 0, got %d at file position 0x%x", length, fp.tell()-4)
    
def read_UN(fp, length):
    """Return a byte string for an DataElement of value 'UN' (unknown)"""
    if length == 0xFFFFFFFFL:
        raise NotImplementedError, "This code has not been tested for 'UN' with unknown length"
        delimiter = 0xFFFEE00DL
        length = length_of_undefined_length(fp, delimiter)
        data_value = fp.read(length)
        read_delimiter_item(fp, delimiter)
        return data_value
    else:
        return fp.read(length)

def read_ATvalue(fp, length):
    """Return an data_element tag as the value of the current Dicom data_element being read"""
    if length == 4:
        return fp.read_tag()
    # length > 4
    if length % 4 != 0:
        logger.warn("Expected length to be multiple of 4 for VR 'AT', got length %d at file position 0x%x", length, fp.tell()-4)
    return MultiValue([fp.read_tag() for i in range(length / 4)])

def _read_file_meta_info(fp):
    """Return the file meta information.
    fp must be set after the 128 byte preamble"""
    magic = fp.read(4)
    if magic != "DICM":
        logger.info("File does not appear to be a DICOM file; 'DICM' header is missing. Call read_file with has_header=False")
        raise IOError, 'File does not appear to be a Dicom file; "DICM" is missing. Try read_file with has_header=False'

    # File meta info is always LittleEndian, Explicit VR. After will change these
    #    to the transfer syntax values set in the meta info
    fp.isLittleEndian = True
    fp.isImplicitVR = False

    GroupLength = read_data_element(fp)
    return read_dataset(fp, GroupLength.value)

def read_file_meta_info(filename):
    """Just get the basic file meta information. Useful for going through
    a series of files to find one which is referenced to a particular SOP."""
    fp = DicomFile(filename, 'rb')
    preamble = fp.read(0x80)
    return _read_file_meta_info(fp)

def read_file(fp):
    """Return a Dataset containing the contents of the Dicom file
    
    fp is either a DicomFile object, or a string containing the file name.
    
    """
    if type(fp) is type(""):
        fp = DicomFile(fp,'rb')
        logger.debug("Reading file '%s'" % fp)
    
    fp.seek(0x80)
    magic = fp.read(4)
    has_header = True
    if magic != "DICM":
        logger.info("File is not a standard DICOM file; 'DICM' header is missing. Assuming no header and continuing")
        has_header = False
    fp.seek(0)
    
    if has_header:
        logger.debug("Reading preamble")
        preamble = fp.read(0x80)
        FileMetaInfo = _read_file_meta_info(fp)
    
        TransferSyntax = FileMetaInfo.TransferSyntaxUID
        if TransferSyntax == ExplicitVRLittleEndian:
            fp.isExplicitVR = True
        elif TransferSyntax == ImplicitVRLittleEndian:
            fp.isImplicitVR = True
        elif TransferSyntax == ExplicitVRBigEndian:
            fp.isExplicitVR = True
            fp.isBigEndian = True
        elif TransferSyntax == DeflatedExplicitVRLittleEndian:
            # See PS3.6-2008 A.5 (p 71) -- when written, the entire dataset following 
            #     the file metadata was prepared the normal way, then "deflate" compression applied.
            #  All that is needed here is to decompress and then use as normal in a file-like object
            zipped = fp.read()            
            # -MAX_WBITS part is from comp.lang.python answer:  http://groups.google.com/group/comp.lang.python/msg/e95b3b38a71e6799
            unzipped = zlib.decompress(zipped, -zlib.MAX_WBITS)
            fp = DicomStringIO(unzipped) # a file-like object that usual code can use as normal
            fp.isExplicitVR = True
            fp.isLittleEndian = True
        else:
            # Any other syntax should be Explicit VR Little Endian,
            #   e.g. all Encapsulated (JPEG etc) are ExplVR-LE by Standard PS 3.5-2008 A.4 (p63)
            fp.isExplicitVR = True
            fp.isLittleEndian = True
    else: # no header -- make assumptions
        fp.isLittleEndian = True
        fp.isImplicitVR = True
    
    logger.debug("Using %s VR, %s Endian transfer syntax" %(("Explicit", "Implicit")[fp.isImplicitVR], ("Big", "Little")[fp.isLittleEndian]))
    
    # Read everything after the File Meta
    ds = read_dataset(fp)
    fp.close()
    
    # Store file info (Big/Little Endian, Expl/Impl VR; preamble) into dataset
    #    so can be used to rewrite same way if desired
    ds.preamble = None # default, set to file's if available below
    ds.has_header = has_header
    if has_header:
        ds.preamble = preamble  
        ds.update(FileMetaInfo) # copy data elements from FileMetaInfo
    ds.isLittleEndian = fp.isLittleEndian
    ds.isExplicitVR = fp.isExplicitVR

    return ds
        

ReadFile = read_file    # For backwards compatibility pydicom version <=0.9.2
readfile = read_file    # forgive a missing underscore

# Readers map a VR to the function to read the value(s).
# for read_numbers, the reader maps to a tuple (function, number format (in python struct module style))
readers = {'UL':(read_numbers,'L'), 'SL':(read_numbers,'l'),
           'US':(read_numbers,'H'), 'SS':(read_numbers, 'h'),
           'FL':(read_numbers,'f'), 'FD':(read_numbers, 'd'),
           'OF':(read_numbers,'f'),
           'OB':read_OBvalue, 'UI':read_UI,
           'SH':read_string,  'DA':read_string, 'TM': read_string,
           'CS':read_string,  'PN':read_PN,     'LO': read_string,
           'IS':read_string,  'DS':read_string, 'AE': read_string,
           'AS':read_string,
           'LT':read_single_string,
           'SQ':read_sequence,
           'UN':read_UN,
           'AT':read_ATvalue,
           'ST':read_string,
           'OW':read_OWvalue,
           'OW/OB':read_OBvalue,# note OW/OB depends on other items, which we don't know at read time
           'OB/OW':read_OBvalue,
           'US or SS':read_OWvalue,
           'US or SS or OW':read_OWvalue,
           'US\US or SS\US':read_OWvalue,
           'DT':read_string,
           'UT':read_single_string,          
           } 
