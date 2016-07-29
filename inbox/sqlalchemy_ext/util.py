import abc
import uuid
import struct
import weakref
import inspect

from bson import json_util, EPOCH_NAIVE
# Monkeypatch to not include tz_info in decoded JSON.
# Kind of a ridiculous solution, but works.
json_util.EPOCH_AWARE = EPOCH_NAIVE

from sqlalchemy import String, Text, event
from sqlalchemy.types import TypeDecorator, BINARY
from sqlalchemy.interfaces import PoolListener
from sqlalchemy.engine import Engine
from sqlalchemy.ext import baked
from sqlalchemy.ext.mutable import Mutable
from sqlalchemy.ext.declarative import DeclarativeMeta
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import Select

from inbox.util.encoding import base36encode, base36decode

from nylas.logging import get_logger
log = get_logger()


MAX_SANE_QUERIES_PER_SESSION = 100
MAX_TEXT_LENGTH = 65535
MAX_MYSQL_INTEGER = 2147483647

bakery = baked.bakery()


query_counts = weakref.WeakKeyDictionary()

@compiles(Select)
def prefix_selects(select, compiler, **kw):
    stack = inspect.stack()
    # Walk the stack backwards until we find the first SQLAlchemy-related
    # frame.
    call_frame = (None, "unavailable", "")
    for i in reversed(range(len(stack))):
        if 'sqlalchemy' in stack[i][1]:
            # Found the first SQLAlchemy frame!
            # The frame just before it is necessarily
            # the calling frame.
            if i + 1 < len(stack):
                call_frame = stack[i + 1]
            break

    comment = "/* {}:{} */".format(call_frame[1], call_frame[2])

    return compiler.visit_select(select.prefix_with(comment), **kw)

@event.listens_for(Engine, "before_cursor_execute")
def before_cursor_execute(conn, cursor, statement,
                          parameters, context, executemany):
    if conn not in query_counts:
        query_counts[conn] = 1
    else:
        query_counts[conn] += 1


@event.listens_for(Engine, 'commit')
def before_commit(conn):
    if query_counts.get(conn, 0) > MAX_SANE_QUERIES_PER_SESSION:
        log.warning('Dubiously many queries per session!',
                    query_count=query_counts.get(conn))


class SQLAlchemyCompatibleAbstractMetaClass(DeclarativeMeta, abc.ABCMeta):
    """Declarative model classes that *also* inherit from an abstract base
    class need a metaclass like this one, in order to prevent metaclass
    conflict errors."""
    pass


class ABCMixin(object):
    """Use this if you want a mixin class which is actually an abstract base
    class, for example in order to enforce that concrete subclasses define
    particular methods or properties."""
    __metaclass__ = SQLAlchemyCompatibleAbstractMetaClass
    __abstract__ = True


# Column Types


# http://docs.sqlalchemy.org/en/rel_0_9/core/types.html#marshal-json-strings
class JSON(TypeDecorator):
    impl = Text

    def process_bind_param(self, value, dialect):
        if value is None:
            return None

        return json_util.dumps(value)

    def process_result_value(self, value, dialect):
        if not value:
            return None

        # Unfortunately loads() is strict about invalid utf-8 whereas dumps()
        # is not. This can result in ValueErrors during decoding - we simply
        # log and return None for now.
        # http://bugs.python.org/issue11489
        try:
            return json_util.loads(value)
        except ValueError:
            log.error('ValueError on decoding JSON', value=value)


def json_field_too_long(value):
    return len(json_util.dumps(value)) > MAX_TEXT_LENGTH


class LittleJSON(JSON):
    impl = String(255)


class BigJSON(JSON):
    # if all characters were 4-byte, this would fit in mysql's MEDIUMTEXT
    impl = Text(4194304)


class Base36UID(TypeDecorator):
    impl = BINARY(16)  # 128 bit unsigned integer

    def process_bind_param(self, value, dialect):
        if not value:
            return None
        return b36_to_bin(value)

    def process_result_value(self, value, dialect):
        return int128_to_b36(value)


# http://bit.ly/1LbMnqu
# Can simply use this as is because though we use bson.json_util, loads()
# dumps() return standard Python dicts like the json.* equivalents
# (because these are simply called under the hood)
class MutableDict(Mutable, dict):

    @classmethod
    def coerce(cls, key, value):
        """ Convert plain dictionaries to MutableDict. """
        if not isinstance(value, MutableDict):
            if isinstance(value, dict):
                return MutableDict(value)

            # this call will raise ValueError
            return Mutable.coerce(key, value)
        else:
            return value

    def __setitem__(self, key, value):
        """ Detect dictionary set events and emit change events. """
        dict.__setitem__(self, key, value)
        self.changed()

    def __delitem__(self, key):
        """ Detect dictionary del events and emit change events. """
        dict.__delitem__(self, key)
        self.changed()

    def update(self, *args, **kwargs):
        for k, v in dict(*args, **kwargs).iteritems():
            self[k] = v

    # To support pickling:
    def __getstate__(self):
        return dict(self)

    def __setstate__(self, state):
        self.update(state)


class MutableList(Mutable, list):

    @classmethod
    def coerce(cls, key, value):
        """Convert plain list to MutableList"""
        if not isinstance(value, MutableList):
            if isinstance(value, list):
                return MutableList(value)

            # this call will raise ValueError
            return Mutable.coerce(key, value)
        else:
            return value

    def __setitem__(self, idx, value):
        list.__setitem__(self, idx, value)
        self.changed()

    def __setslice__(self, start, stop, values):
        list.__setslice__(self, start, stop, values)
        self.changed()

    def __delitem__(self, idx):
        list.__delitem__(self, idx)
        self.changed()

    def __delslice__(self, start, stop):
        list.__delslice__(self, start, stop)
        self.changed()

    def append(self, value):
        list.append(self, value)
        self.changed()

    def insert(self, idx, value):
        list.insert(self, idx, value)
        self.changed()

    def extend(self, values):
        list.extend(self, values)
        self.changed()

    def pop(self, *args, **kw):
        value = list.pop(self, *args, **kw)
        self.changed()
        return value

    def remove(self, value):
        list.remove(self, value)
        self.changed()


def int128_to_b36(int128):
    """ int128: a 128 bit unsigned integer
        returns a base-36 string representation
    """
    if not int128:
        return None
    assert len(int128) == 16, "should be 16 bytes (128 bits)"
    a, b = struct.unpack('>QQ', int128)  # uuid() is big-endian
    pub_id = (a << 64) | b
    return base36encode(pub_id).lower()


def b36_to_bin(b36_string):
    """ b36_string: a base-36 encoded string
        returns binary 128 bit unsigned integer
    """
    int128 = base36decode(b36_string)
    MAX_INT64 = 0xFFFFFFFFFFFFFFFF
    return struct.pack(
        '>QQ',
        (int128 >> 64) & MAX_INT64,
        int128 & MAX_INT64)


def generate_public_id():
    """ Returns a base-36 string UUID """
    u = uuid.uuid4().bytes
    return int128_to_b36(u)


# Other utilities

# My good old friend Enrico to the rescue:
# http://www.enricozini.org/2012/tips/sa-sqlmode-traditional/
#
# We set sql-mode=traditional on the server side as well, but enforce at the
# application level to be extra safe.
#
# Without this, MySQL will silently insert invalid values in the database if
# not running with sql-mode=traditional.
class ForceStrictMode(PoolListener):

    def connect(self, dbapi_con, connection_record):
        cur = dbapi_con.cursor()
        cur.execute("SET SESSION sql_mode='TRADITIONAL'")
        cur = None


def maybe_refine_query(query, subquery):
    if subquery is None:
        return query
    return query.join(subquery.subquery())


def safer_yield_per(query, id_field, start_id, count):
    """Incautious execution of 'for result in query.yield_per(N):' may cause
    slowness or OOMing over large tables. This is a less general but less
    dangerous alternative.

    Parameters
    ----------
    query: sqlalchemy.Query
        The query to yield windowed results from.
    id_field: A SQLAlchemy attribute to use for windowing. E.g.,
        `Transaction.id`
    start_id: The value of id_field at which to start iterating.
    count: int
        The number of results to fetch at a time.
    """
    cur_id = start_id
    while True:
        results = query.filter(id_field >= cur_id).order_by(id_field).\
            limit(count).all()
        if not results:
            return
        for result in results:
            yield result
        cur_id = results[-1].id + 1
