# -*- coding: utf-8 -*-

"""
@author: leohowell
@date: 2018/2/11
"""

import json
import time
import logging
import uuid
from functools import wraps
from contextlib import contextmanager

import redis


NAMESPACE_DELIMITER = b':'
PREFIX_DELIMITER = NAMESPACE_DELIMITER * 2


logger = logging.getLogger(__name__)



def redis_lru_cache(max_size=1024, expiration=15 * 60, client=None,
                    cache=None, typed_hash_args=False):
    """
    >>> @redis_lru_cache(20, 1)
    ... def f(x):
    ...    print("Calling f(" + str(x) + ")")
    ...    return x
    >>> f(3)
    Calling f(3)
    3
    >>> f(3)
    3
    """

    def _hash_args(args, kwargs):
        return hash(
            (hash(args),
             hash(frozenset(kwargs.items()))
             )
        )

    def _typed_hash_args(args, kwargs):
        return hash((
            _hash_args(args, kwargs),
            hash(type(x) for x in args),
            hash(type(x) for x in kwargs.values()),
        ))

    def wrapper(func):
        if cache is None:
            unique_key = NAMESPACE_DELIMITER.join(
                x.encode().replace(b'.', NAMESPACE_DELIMITER)
                for x in (func.__module__, func.__qualname__)
            )
            lru_cache = RedisLRUCacheDict(
                unique_key, max_size, expiration, client
            )
        else:
            lru_cache = cache

        _arg_hasher = _typed_hash_args if typed_hash_args else _hash_args

        @wraps(func)
        def inner(*args, **kwargs):
            try:
                key = hex(_arg_hasher(args, kwargs))
            except TypeError:
                raise RuntimeError(
                    'All arguments to lru-cached functions must be hashable.'
                )

            try:
                return lru_cache[key]
            except KeyError:
                value = func(*args, **kwargs)
                lru_cache[key] = value
                return value

        return inner

    return wrapper


def joint_key(method):
    @wraps(method)
    def wrapper(self, key, *args, **kwargs):
        key = b'lru-value:' + self.unique_key + PREFIX_DELIMITER + key.encode()
        return method(self, key, *args, **kwargs)
    return wrapper


@contextmanager
def redis_pipeline(client):
    p = client.pipeline()
    yield p
    p.execute()


class RedisLRUCacheDict:
    """ A dictionary-like object, supporting LRU caching semantics.
    >>> d = RedisLRUCacheDict('unique_key', max_size=3, expiration=1)
    >>> d['foo'] = 'bar'
    >>> x = d['foo']
    >>> print(x)
    bar
    >>> import time
    >>> time.sleep(1.1) # 1.1 seconds > 1 second cache expiry of d
    >>> d['foo']
    Traceback (most recent call last):
        ...
    KeyError: 'foo'
    >>> d['a'] = 'A'
    >>> d['b'] = 'B'
    >>> d['c'] = 'C'
    >>> d['d'] = 'D'
    >>> d['a'] # Should return value error, since we exceeded the max cache size
    Traceback (most recent call last):
        ...
    KeyError: 'a'
    """

    EXPIRATION_STAT_KEY = 30 * 86400  # 30 day

    HIT = 'HIT'
    MISS = 'MISS'
    POP = 'POP'
    SET = 'SET'
    DEL = 'DEL'
    DUMPS_ERROR = 'DUMPS_ERROR'
    LOADS_ERROR = 'LOADS_ERROR'

    ONCE_CLEAN_RATIO = 0.1

    def __init__(self, unique_key=None, max_size=1024, expiration=15*60,
                 client=None, clear_stat=False):

        if isinstance(unique_key, str):
            unique_key = unique_key.encode()

        if unique_key is not None:
            if PREFIX_DELIMITER in unique_key:
                raise ValueError('Invalid unique key: {}'.format(unique_key))
            self.unique_key = unique_key
        else:
            self.unique_key = uuid.uuid4().bytes
            logger.debug('Generated `unique key`: {}'.format(self.unique_key))

        self.max_size = max_size
        self.expiration = expiration
        self.client = client or redis.StrictRedis()

        self.access_key = 'lru-access:{}'.format(self.unique_key)  # sorted set
        self.stat_key = 'lru-stat:{}'.format(self.unique_key)      # hash set

        self.once_clean_size = int(self.max_size * self.ONCE_CLEAN_RATIO)

        if clear_stat:
            self.client.delete(self.stat_key)

    def report_usage(self):
        return self.client.hgetall(self.stat_key)

    @property
    def size(self):
        return self.client.zcard(self.access_key)

    @joint_key
    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def _ensure_room(self):
        if self.size < self.max_size:
            return True

        keys = self.client.zrange(self.access_key, 0, self.once_clean_size)

        with redis_pipeline(self.client) as p:
            for k in keys:
                p.delete(k)
                p.zrem(self.access_key, k)
            p.hincrby(self.stat_key, self.POP, len(keys))

        return False

    @joint_key
    def __setitem__(self, key, value):
        try:
            value = json.dumps(value)
        except Exception:  # here too broad exception clause, just ignore it
            with redis_pipeline(self.client) as p:
                p.hincrby(self.stat_key, self.DUMPS_ERROR, 1)
                p.expire(self.stat_key, self.EXPIRATION_STAT_KEY)
            return

        self._ensure_room()

        with redis_pipeline(self.client) as p:
            p.setex(key, self.expiration, value)

            p.zadd(self.access_key, time.time(), key)
            p.expire(self.access_key, self.expiration)

            p.hincrby(self.stat_key, self.SET, 1)
            p.expire(self.stat_key, self.EXPIRATION_STAT_KEY)

    @joint_key
    def __delitem__(self, key):
        with redis_pipeline(self.client) as p:
            p.delete(key)
            p.zrem(self.access_key, key)
            p.expire(self.access_key, self.expiration)

            p.hincrby(self.stat_key, self.DEL, 1)
            p.expire(self.stat_key, self.EXPIRATION_STAT_KEY)

    @joint_key
    def __getitem__(self, key):
        value = self.client.get(key)
        if value is None:
            with redis_pipeline(self.client) as p:
                p.hincrby(self.stat_key, self.MISS, 1)
                p.expire(self.stat_key, self.EXPIRATION_STAT_KEY)
                p.execute()

            real_key = key.split(PREFIX_DELIMITER, 1)[1]
            raise KeyError(real_key.decode())
        else:
            try:
                value = json.loads(value)
            except Exception:
                with redis_pipeline(self.client) as p:
                    p.delete(key)
                    p.hincrby(self.stat_key, self.LOADS_ERROR, 1)
                    p.expire(self.stat_key, self.EXPIRATION_STAT_KEY)
                raise KeyError(key)

            with redis_pipeline(self.client) as p:
                p.zadd(self.access_key, time.time(), key)
                p.expire(self.access_key, self.expiration)

                p.hincrby(self.stat_key, self.HIT, 1)
                p.expire(self.stat_key, self.EXPIRATION_STAT_KEY)

            return value

    @joint_key
    def __contains__(self, key):
        return bool(self.client.exist(key))


if __name__ == "__main__":
    import doctest

    doctest.testmod()