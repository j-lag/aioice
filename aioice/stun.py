

import asyncio
import binascii
import enum
import hmac
import logging
from collections import OrderedDict
from ipaddress import IPv4Address, IPv6Address
from struct import pack, unpack

COOKIE = 0x2112a442
FINGERPRINT_XOR = 0x5354554e
HEADER_LENGTH = 20
IPV4_PROTOCOL = 1
IPV6_PROTOCOL = 2

RETRY_INTERVAL = 0.5
RETRY_MAX = 7


logger = logging.getLogger('stun')


def xor_address(data, transaction_id):
    xpad = pack('!HI', COOKIE >> 16, COOKIE) + transaction_id
    xdata = data[0:2]
    for i in range(2, len(data)):
        xdata += int.to_bytes(data[i] ^ xpad[i - 2], 1, 'big', signed=False)
    return xdata


def pack_address(value, **kwargs):
    if isinstance(value[0], IPv4Address):
        protocol = IPV4_PROTOCOL
    elif isinstance(value[0], IPv6Address):
        protocol = IPV6_PROTOCOL
    else:
        raise ValueError('Value must be an IPv4Address or IPv6Address')
    return pack('!BBH', 0, protocol, value[1]) + value[0].packed


def pack_bytes(value):
    return value


def pack_none(value):
    return b''


def pack_string(value):
    return value.encode('utf8')


def pack_unsigned(value):
    return pack('!I', value)


def pack_xor_address(value, transaction_id):
    return xor_address(pack_address(value), transaction_id)


def unpack_address(data):
    if len(data) < 4:
        raise ValueError('STUN address length is less than 4 bytes')
    reserved, protocol, port = unpack('!BBH', data[0:4])
    address = data[4:]
    if protocol == IPV4_PROTOCOL:
        if len(address) != 4:
            raise ValueError('STUN address has invalid length for IPv4')
        return (IPv4Address(address), port)
    elif protocol == IPV6_PROTOCOL:
        if len(address) != 16:
            raise ValueError('STUN address has invalid length for IPv6')
        return (IPv6Address(address), port)
    else:
        raise ValueError('STUN address protocol is unsupported')


def unpack_xor_address(data, transaction_id):
    return unpack_address(xor_address(data, transaction_id))


def unpack_bytes(data):
    return data


def unpack_none(data):
    return None


def unpack_string(data):
    return data.decode('utf8')


def unpack_unsigned(data):
    return unpack('!I', data)[0]


ATTRIBUTES = [
    (0x0001, 'MAPPED-ADDRESS', pack_address, unpack_address),
    (0x0003, 'CHANGE-REQUEST', pack_unsigned, unpack_unsigned),
    (0x0004, 'SOURCE-ADDRESS', pack_address, unpack_address),
    (0x0005, 'CHANGED-ADDRESS', pack_address, unpack_address),
    (0x0006, 'USERNAME', pack_string, unpack_string),
    (0x0008, 'MESSAGE-INTEGRITY', pack_bytes, unpack_bytes),
    (0x0012, 'XOR-PEER-ADDRESS', pack_xor_address, unpack_xor_address),
    (0x0014, 'REALM', pack_string, unpack_string),
    (0x0015, 'NONCE', pack_bytes, unpack_bytes),
    (0x0016, 'XOR-RELAYED-ADDRESS', pack_xor_address, unpack_xor_address),
    (0x0020, 'XOR-MAPPED-ADDRESS', pack_xor_address, unpack_xor_address),
    (0x0024, 'PRIORITY', pack_unsigned, unpack_unsigned),
    (0x0025, 'USE-CANDIDATE', pack_none, unpack_none),
    (0x8022, 'SOFTWARE', pack_string, unpack_string),
    (0x8028, 'FINGERPRINT', pack_unsigned, unpack_unsigned),
    (0x8029, 'ICE-CONTROLLED', pack_bytes, unpack_bytes),
    (0x802a, 'ICE-CONTROLLING', pack_bytes, unpack_bytes),
    (0x802b, 'RESPONSE-ORIGIN', pack_address, unpack_address),
    (0x802c, 'OTHER-ADDRESS', pack_address, unpack_address),
]

ATTRIBUTES_BY_TYPE = {}
ATTRIBUTES_BY_NAME = {}
for attr in ATTRIBUTES:
    ATTRIBUTES_BY_TYPE[attr[0]] = attr
    ATTRIBUTES_BY_NAME[attr[1]] = attr


class Class(enum.IntEnum):
    REQUEST = 0x000
    INDICATION = 0x010
    RESPONSE = 0x100
    ERROR = 0x110


class Method(enum.IntEnum):
    BINDING = 0x1


class Message(object):
    def __init__(self, message_method, message_class, transaction_id,
                 attributes=None):
        self.message_method = message_method
        self.message_class = message_class
        self.transaction_id = transaction_id
        self.attributes = attributes or OrderedDict()

    def add_fingerprint(self):
        data = bytes(self)
        # increase length by 8
        data = data[0:2] + pack('!H', len(data) - HEADER_LENGTH + 8) + data[4:]
        self.attributes['FINGERPRINT'] = binascii.crc32(data) ^ FINGERPRINT_XOR

    def add_message_integrity(self, key):
        data = bytes(self)
        # increase length by 24
        data = data[0:2] + pack('!H', len(data) - HEADER_LENGTH + 24) + data[4:]
        self.attributes['MESSAGE-INTEGRITY'] = hmac.new(key, data, 'sha1').digest()

    def __bytes__(self):
        data = b''
        for attr_name, attr_value in self.attributes.items():
            attr_type, _, attr_pack, attr_unpack = ATTRIBUTES_BY_NAME[attr_name]
            if attr_pack == pack_xor_address:
                v = attr_pack(attr_value, self.transaction_id)
            else:
                v = attr_pack(attr_value)

            attr_len = len(v)
            pad_len = 4 * ((attr_len + 3) // 4) - attr_len
            data += pack('!HH', attr_type, attr_len) + v + (b'\x00' * pad_len)
        return pack('!HHI12s',
                    self.message_method | self.message_class,
                    len(data), COOKIE, self.transaction_id) + data

    def __repr__(self):
        return 'Message(message_method=%s, message_class=%s, transaction_id=%s)' % (
            self.message_method,
            self.message_class,
            self.transaction_id,
        )


class Transaction:
    def __init__(self, request, addr, protocol):
        self.__addr = addr
        self.__future = asyncio.Future()
        self.__request = request
        self.__timeout_handle = None
        self.__protocol = protocol
        self.__tries = 0

    @property
    def request(self):
        return self.__request

    def message_received(self, message, addr):
        logger.debug('client < %s' % repr(message))
        self.__timeout_handle.cancel()
        self.__future.set_result(message)

    async def run(self):
        self.__retry()
        return await self.__future

    def __retry(self):
        if self.__tries >= RETRY_MAX:
            logger.debug('timeout')
            self.__future.set_exception(Exception('Timeout'))
            return

        logger.debug('client > %s' % repr(self.__request))
        self.__protocol.send(self.__request, self.__addr)

        if self.__tries:
            self.__timeout_delay = 2 * self.__timeout_delay
        else:
            self.__timeout_delay = RETRY_INTERVAL

        loop = asyncio.get_event_loop()
        self.__timeout_handle = loop.call_later(self.__timeout_delay, self.__retry)
        self.__tries += 1


def parse_message(data):
    if len(data) < HEADER_LENGTH:
        raise ValueError('STUN message length is less than 20 bytes')
    message_type, length, cookie, transaction_id = unpack('!HHI12s', data[0:HEADER_LENGTH])
    if len(data) != HEADER_LENGTH + length:
        raise ValueError('STUN message length does not match')

    attributes = OrderedDict()
    pos = HEADER_LENGTH
    while pos <= len(data) - 4:
        attr_type, attr_len = unpack('!HH', data[pos:pos + 4])
        v = data[pos + 4:pos + 4 + attr_len]
        pad_len = 4 * ((attr_len + 3) // 4) - attr_len
        if attr_type in ATTRIBUTES_BY_TYPE:
            _, attr_name, attr_pack, attr_unpack = ATTRIBUTES_BY_TYPE[attr_type]
            if attr_unpack == unpack_xor_address:
                attributes[attr_name] = attr_unpack(v, transaction_id=transaction_id)
            else:
                attributes[attr_name] = attr_unpack(v)
        pos += 4 + attr_len + pad_len
    return Message(
        message_method=message_type & 0x3eef,
        message_class=message_type & 0x0100,
        transaction_id=transaction_id,
        attributes=attributes)