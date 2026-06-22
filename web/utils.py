#!/usr/bin/env python3
"""
General Utilities（通用工具集）
(part of web.py)

【功能层】提供 web.py 框架运行所需的基础工具：数据容器、函数缓存、字符串处理、
         线程本地存储、邮件发送等。
【设计层】大量使用 Python 高级特性：魔术方法重载、装饰器、元类、线程局部变量、
         描述符协议等，体现了"小而美"的函数式 + 面向对象混合风格。
【上下文层】被框架所有其他模块依赖，是 web.py 的基础设施层。
"""

import datetime
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from io import StringIO
from threading import local as threadlocal

from .py3helpers import iteritems, itervalues

__all__ = [
    "Storage",
    "storage",
    "storify",
    "Counter",
    "counter",
    "iters",
    "rstrips",
    "lstrips",
    "strips",
    "safeunicode",
    "safestr",
    "timelimit",
    "Memoize",
    "memoize",
    "re_compile",
    "re_subm",
    "group",
    "uniq",
    "iterview",
    "IterBetter",
    "iterbetter",
    "safeiter",
    "safewrite",
    "dictreverse",
    "dictfind",
    "dictfindall",
    "dictincr",
    "dictadd",
    "requeue",
    "restack",
    "listget",
    "intget",
    "datestr",
    "numify",
    "denumify",
    "commify",
    "dateify",
    "nthstr",
    "cond",
    "CaptureStdout",
    "capturestdout",
    "Profile",
    "profile",
    "tryall",
    "ThreadedDict",
    "threadeddict",
    "autoassign",
    "to36",
    "sendmail",
]


class Storage(dict):
    """
    【功能层】Storage 是增强型字典，同时支持属性访问（obj.foo）和下标访问（obj['foo']）。
    【设计层】继承 dict 并重载 __getattr__ / __setattr__ / __delattr__ 三个魔术方法，
             将属性操作代理到字典操作，是 Python "鸭子类型" 和描述符协议的经典应用。
             相比 namedtuple 更灵活（可增删键），相比普通 dict 访问更简洁。
    【上下文层】贯穿整个框架：web.ctx（请求上下文）、web.config（全局配置）、
             数据库查询结果行、会话数据等都使用 Storage 或其子类。

    A Storage object is like a dictionary except `obj.foo` can be used
    in addition to `obj['foo']`.

        >>> o = storage(a=1)
        >>> o.a
        1
        >>> o['a']
        1
        >>> o.a = 2
        >>> o['a']
        2
        >>> del o.a
        >>> o.a
        Traceback (most recent call last):
            ...
        AttributeError: 'a'

    """

    def __getattr__(self, key):
        # 【功能层】属性访问时转为字典键查找；KeyError 转换成更友好的 AttributeError
        # 【设计层】利用 Python 属性查找链：先找实例/类属性，找不到才调用 __getattr__
        try:
            return self[key]
        except KeyError as k:
            raise AttributeError(k)

    def __setattr__(self, key, value):
        # 【功能层】属性赋值时同步写入字典，保持两种访问方式的一致性
        self[key] = value

    def __delattr__(self, key):
        # 【功能层】属性删除时同步从字典中删除对应键
        try:
            del self[key]
        except KeyError as k:
            raise AttributeError(k)

    def __repr__(self):
        # 【功能层】提供带类型标识的字符串表示，便于调试时区分 Storage 与普通 dict
        return "<Storage " + dict.__repr__(self) + ">"


storage = Storage  # 小写别名，方便 `web.storage(a=1)` 的调用风格


def storify(mapping, *requireds, **defaults):
    """
    【功能层】从字典 mapping 创建 Storage 对象；requireds 是必须存在的键，
             defaults 提供缺省值和类型提示。若 mapping 中的值是列表，
             默认取最后一个元素（表单多值场景的合理默认）。
    【设计层】利用可变参数 (*requireds, **defaults) 实现灵活的键声明，
             结合 getattr(x, 'value', x) 自动解包表单字段对象（如 cgi.FieldStorage），
             体现了"约定优于配置"的设计思想。
    【上下文层】web.input() 内部调用此函数将原始 HTTP 参数字典转换为可属性访问的对象。

    Creates a `storage` object from dictionary `mapping`, raising `KeyError` if
    d doesn't have all of the keys in `requireds` and using the default
    values for keys found in `defaults`.

    For example, `storify({'a':1, 'c':3}, b=2, c=0)` will return the equivalent of
    `storage({'a':1, 'b':2, 'c':3})`.

    If a `storify` value is a list (e.g. multiple values in a form submission),
    `storify` returns the last element of the list, unless the key appears in
    `defaults` as a list. Thus:

        >>> storify({'a':[1, 2]}).a
        2
        >>> storify({'a':[1, 2]}, a=[]).a
        [1, 2]
        >>> storify({'a':1}, a=[]).a
        [1]
        >>> storify({}, a=[]).a
        []

    Similarly, if the value has a `value` attribute, `storify will return _its_
    value, unless the key appears in `defaults` as a dictionary.

        >>> storify({'a':storage(value=1)}).a
        1
        >>> storify({'a':storage(value=1)}, a={}).a
        <Storage {'value': 1}>
        >>> storify({}, a={}).a
        {}

    """
    _unicode = defaults.pop("_unicode", False)

    # if _unicode is callable object, use it convert a string to unicode.
    to_unicode = safeunicode
    if _unicode is not False and hasattr(_unicode, "__call__"):
        to_unicode = _unicode

    def unicodify(s):
        if _unicode and isinstance(s, str):
            return to_unicode(s)
        else:
            return s

    def getvalue(x):
        if hasattr(x, "file") and hasattr(x, "raw"):
            return x.file.read()
        else:
            return unicodify(getattr(x, "value", x))

    stor = Storage()
    for key in requireds + tuple(mapping.keys()):
        value = mapping[key]
        if isinstance(value, list):
            if isinstance(defaults.get(key), list):
                value = [getvalue(x) for x in value]
            else:
                value = value[-1]
        if not isinstance(defaults.get(key), dict):
            value = getvalue(value)
        if isinstance(defaults.get(key), list) and not isinstance(value, list):
            value = [value]

        setattr(stor, key, value)

    for key, value in iteritems(defaults):
        result = value
        if hasattr(stor, key):
            result = stor[key]
        if value == () and not isinstance(result, tuple):
            result = (result,)
        setattr(stor, key, result)

    return stor


class Counter(storage):
    """
    【功能层】计数器，记录每个键被 add() 添加的次数，并提供统计查询接口。
    【设计层】继承 storage（即 Storage/dict），用字典存储计数，
             是"组合优于继承"与"继承均可"场景下选用继承的简洁示范。
    【上下文层】框架内部可用于统计请求、错误次数等；也作为工具类暴露给用户代码。

    Keeps count of how many times something is added.

    >>> c = counter()
    >>> c.add('x')
    >>> c.add('x')
    >>> c.add('x')
    >>> c.add('x')
    >>> c.add('x')
    >>> c.add('y')
    >>> c['y']
    1
    >>> c['x']
    5
    >>> c.most()
    ['x']
    """

    def add(self, n):
        self.setdefault(n, 0)
        self[n] += 1

    def most(self):
        """Returns the keys with maximum count."""
        m = max(itervalues(self))
        return [k for k, v in iteritems(self) if v == m]

    def least(self):
        """Returns the keys with minimum count."""
        m = min(self.itervalues())
        return [k for k, v in iteritems(self) if v == m]

    def percent(self, key):
        """Returns what percentage a certain key is of all entries.

        >>> c = counter()
        >>> c.add('x')
        >>> c.add('x')
        >>> c.add('x')
        >>> c.add('y')
        >>> c.percent('x')
        0.75
        >>> c.percent('y')
        0.25
        """
        return float(self[key]) / sum(self.values())

    def sorted_keys(self):
        """Returns keys sorted by value.

        >>> c = counter()
        >>> c.add('x')
        >>> c.add('x')
        >>> c.add('y')
        >>> c.sorted_keys()
        ['x', 'y']
        """
        return sorted(self.keys(), key=lambda k: self[k], reverse=True)

    def sorted_values(self):
        """Returns values sorted by value.

        >>> c = counter()
        >>> c.add('x')
        >>> c.add('x')
        >>> c.add('y')
        >>> c.sorted_values()
        [2, 1]
        """
        return [self[k] for k in self.sorted_keys()]

    def sorted_items(self):
        """Returns items sorted by value.

        >>> c = counter()
        >>> c.add('x')
        >>> c.add('x')
        >>> c.add('y')
        >>> c.sorted_items()
        [('x', 2), ('y', 1)]
        """
        return [(k, self[k]) for k in self.sorted_keys()]

    def __repr__(self):
        return "<Counter " + dict.__repr__(self) + ">"


counter = Counter

iters = [list, tuple, set, frozenset]


class _hack(tuple):
    pass


iters = _hack(iters)
iters.__doc__ = """
A list of iterable items (like lists, but not strings). Includes whichever
of lists, tuples, sets, and Sets are available in this version of Python.
"""


def _strips(direction, text, remove):
    if isinstance(remove, iters):
        for subr in remove:
            text = _strips(direction, text, subr)
        return text

    if direction == "l":
        if text.startswith(remove):
            return text[len(remove) :]
    elif direction == "r":
        if text.endswith(remove):
            return text[: -len(remove)]
    else:
        raise ValueError("Direction needs to be r or l.")
    return text


def rstrips(text, remove):
    """
    removes the string `remove` from the right of `text`

        >>> rstrips("foobar", "bar")
        'foo'

    """
    return _strips("r", text, remove)


def lstrips(text, remove):
    """
    removes the string `remove` from the left of `text`

        >>> lstrips("foobar", "foo")
        'bar'
        >>> lstrips('http://foo.org/', ['http://', 'https://'])
        'foo.org/'
        >>> lstrips('FOOBARBAZ', ['FOO', 'BAR'])
        'BAZ'
        >>> lstrips('FOOBARBAZ', ['BAR', 'FOO'])
        'BARBAZ'

    """
    return _strips("l", text, remove)


def strips(text, remove):
    """
    removes the string `remove` from the both sides of `text`

        >>> strips("foobarfoo", "foo")
        'bar'

    """
    return rstrips(lstrips(text, remove), remove)


def safestr(obj, encoding="utf-8"):
    r"""
    Converts any given object to utf-8 encoded string.

        >>> safestr('hello')
        'hello'
        >>> safestr(2)
        '2'
    """

    if obj and hasattr(obj, "__next__"):
        return [safestr(i) for i in obj]
    else:
        return str(obj)


# Since Python3, utf-8 encoded strings and unicode strings are the same thing
safeunicode = safestr


def timelimit(timeout):
    """
    【功能层】装饰器工厂：为被装饰函数添加执行时间上限，超时抛出 RuntimeError。
    【设计层】经典的"装饰器工厂"三层嵌套结构：timelimit(n) -> _1(func) -> _2(*args)。
             内部用 threading.Thread 在独立线程运行目标函数，主线程调用 join(timeout)
             等待；若线程仍存活则判定超时。
             注意：线程无法被强制终止，超时后目标函数仍在后台执行（文档中有说明）。
    【上下文层】web.py 使用此装饰器保护可能因外部依赖（DB、网络）而卡死的操作；
             同时作为 memoize 的后台刷新控制手段。

    A decorator to limit a function to `timeout` seconds, raising `TimeoutError`
    if it takes longer.

        >>> import time
        >>> def meaningoflife():
        ...     time.sleep(.2)
        ...     return 42
        >>>
        >>> timelimit(.1)(meaningoflife)()
        Traceback (most recent call last):
            ...
        RuntimeError: took too long
        >>> timelimit(1)(meaningoflife)()
        42

    _Caveat:_ The function isn't stopped after `timeout` seconds but continues
    executing in a separate thread. (There seems to be no way to kill a thread.)

    inspired by <http://aspn.activestate.com/ASPN/Cookbook/Python/Recipe/473878>
    """

    def _1(function):
        # 【设计层】第二层闭包，捕获 function，返回真正的包装函数 _2
        def _2(*args, **kw):
            # 【设计层】第三层闭包，每次调用时创建独立的 Dispatch 线程
            class Dispatch(threading.Thread):
                # 【设计层】内部类线程，daemon=True 保证主进程退出时不被此线程阻塞
                def __init__(self):
                    threading.Thread.__init__(self)
                    self.result = None   # 保存函数正常返回值
                    self.error = None    # 保存函数抛出的异常信息

                    self.daemon = True   # 守护线程：主线程结束时自动回收
                    self.start()         # 构造时即启动，减少一次显式调用

                def run(self):
                    # 【功能层】在新线程中执行目标函数，捕获所有异常以便主线程重新抛出
                    try:
                        self.result = function(*args, **kw)
                    except:
                        self.error = sys.exc_info()  # (type, value, traceback) 三元组

            c = Dispatch()
            c.join(timeout)          # 主线程最多等待 timeout 秒
            if c.is_alive():
                raise RuntimeError("took too long")   # 超时：线程仍在运行
            if c.error:
                raise c.error[1]     # 把子线程的异常在主线程重新抛出
            return c.result

        return _2

    return _1


class Memoize:
    """
    【功能层】函数结果缓存（记忆化）：对相同参数的调用直接返回缓存结果，避免重复计算。
             支持过期时间（expires 秒后重新计算）和后台刷新（background=True 时
             在独立线程更新缓存，调用方立即得到旧值，不阻塞）。
    【设计层】实现了 __call__ 协议，使实例可以像函数一样调用（可调用对象模式）。
             用 threading.Lock 保证缓存写入的线程安全；后台刷新时使用非阻塞 acquire
             避免多线程同时触发刷新。缓存 key 为 (args, tuple(kwargs.items()))，
             要求参数必须可哈希。
    【上下文层】`re_compile = memoize(re.compile)` 是框架内最典型的用例：
             缓存编译好的正则对象，大幅减少重复编译开销。

    'Memoizes' a function, caching its return values for each input.
    If `expires` is specified, values are recalculated after `expires` seconds.
    If `background` is specified, values are recalculated in a separate thread.

        >>> calls = 0
        >>> def howmanytimeshaveibeencalled():
        ...     global calls
        ...     calls += 1
        ...     return calls
        >>> fastcalls = memoize(howmanytimeshaveibeencalled)
        >>> howmanytimeshaveibeencalled()
        1
        >>> howmanytimeshaveibeencalled()
        2
        >>> fastcalls()
        3
        >>> fastcalls()
        3
        >>> import time
        >>> fastcalls = memoize(howmanytimeshaveibeencalled, .1, background=False)
        >>> fastcalls()
        4
        >>> fastcalls()
        4
        >>> time.sleep(.2)
        >>> fastcalls()
        5
        >>> def slowfunc():
        ...     time.sleep(.1)
        ...     return howmanytimeshaveibeencalled()
        >>> fastcalls = memoize(slowfunc, .2, background=True)
        >>> fastcalls()
        6
        >>> timelimit(.05)(fastcalls)()
        6
        >>> time.sleep(.2)
        >>> timelimit(.05)(fastcalls)()
        6
        >>> timelimit(.05)(fastcalls)()
        6
        >>> time.sleep(.2)
        >>> timelimit(.05)(fastcalls)()
        7
        >>> fastcalls = memoize(slowfunc, None, background=True)
        >>> threading.Thread(target=fastcalls).start()
        >>> time.sleep(.01)
        >>> fastcalls()
        9
    """

    def __init__(self, func, expires=None, background=True):
        self.func = func
        self.cache = {}
        self.expires = expires
        self.background = background
        self.running = {}
        self.running_lock = threading.Lock()

    def __call__(self, *args, **keywords):
        # 【功能层】每次调用时，先构建缓存键，检查是否命中缓存
        key = (args, tuple(keywords.items()))
        # 【设计层】用 running_lock 保护 running 字典本身的创建，
        #          running[key] 是针对单个缓存槽的更新锁
        with self.running_lock:
            if not self.running.get(key):
                self.running[key] = threading.Lock()

        def update(block=False):
            # 【功能层】执行原始函数并更新缓存；block=True 时阻塞等待锁（初次计算），
            #          block=False 时尝试获取锁（后台刷新，获取不到则放弃此次更新）
            if self.running[key].acquire(block):
                try:
                    self.cache[key] = (self.func(*args, **keywords), time.time())
                finally:
                    self.running[key].release()

        if key not in self.cache:
            update(block=True)    # 首次调用：同步阻塞计算
        elif self.expires and (time.time() - self.cache[key][1]) > self.expires:
            if self.background:
                threading.Thread(target=update).start()  # 后台刷新，不阻塞调用方
            else:
                update()          # 同步刷新
        return self.cache[key][0]  # 返回缓存值（tuple 第 0 项）


memoize = Memoize  # 小写别名

# 【功能层】缓存版的正则编译函数；相同 pattern 只编译一次
# 【设计层】memoize(re.compile) 是函数式编程中"高阶函数"的典型应用：
#          把一个普通函数包装成带缓存能力的新函数，完全无需修改原函数
re_compile = memoize(re.compile)
re_compile.__doc__ = """
A memoized version of re.compile.
（记忆化正则编译：对相同 pattern 的 re.compile 调用只执行一次，结果复用）
"""


class _re_subm_proxy:
    def __init__(self):
        self.match = None

    def __call__(self, match):
        self.match = match
        return ""


def re_subm(pat, repl, string):
    """
    Like re.sub, but returns the replacement _and_ the match object.

        >>> t, m = re_subm('g(oo+)fball', r'f\\1lish', 'goooooofball')
        >>> t
        'foooooolish'
        >>> m.groups()
        ('oooooo',)
    """
    compiled_pat = re_compile(pat)
    proxy = _re_subm_proxy()
    compiled_pat.sub(proxy.__call__, string)
    return compiled_pat.sub(repl, string), proxy.match


def group(seq, size):
    """
    【功能层】将序列按固定大小分组，返回生成器（惰性求值）。
    【设计层】使用生成器表达式（generator expression），比返回列表更节省内存；
             切片操作 seq[i:i+size] 自然处理尾部不足 size 的情况。
    【上下文层】application.init_mapping 用此函数将 (url_pattern, handler, ...) 
             的平铺元组转为 [(pattern, handler), ...] 的路由表。

    Returns an iterator over a series of lists of length size from iterable.

        >>> list(group([1,2,3,4], 2))
        [[1, 2], [3, 4]]
        >>> list(group([1,2,3,4,5], 2))
        [[1, 2], [3, 4], [5]]
    """
    return (seq[i : i + size] for i in range(0, len(seq), size))


def uniq(seq, key=None):
    """
    Removes duplicate elements from a list while preserving the order of the rest.

        >>> uniq([9,0,2,1,0])
        [9, 0, 2, 1]

    The value of the optional `key` parameter should be a function that
    takes a single argument and returns a key to test the uniqueness.

        >>> uniq(["Foo", "foo", "bar"], key=lambda s: s.lower())
        ['Foo', 'bar']
    """
    key = key or (lambda x: x)
    seen = set()
    result = []
    for v in seq:
        k = key(v)
        if k in seen:
            continue
        seen.add(k)
        result.append(v)
    return result


def iterview(x):
    """
    Takes an iterable `x` and returns an iterator over it
    which prints its progress to stderr as it iterates through.
    """
    WIDTH = 70

    def plainformat(n, lenx):
        return "%5.1f%% (%*d/%d)" % ((float(n) / lenx) * 100, len(str(lenx)), n, lenx)

    def bars(size, n, lenx):
        val = int((float(n) * size) / lenx + 0.5)
        if size - val:
            spacing = ">" + (" " * (size - val))[1:]
        else:
            spacing = ""
        return "[{}{}]".format("=" * val, spacing)

    def eta(elapsed, n, lenx):
        if n == 0:
            return "--:--:--"
        if n == lenx:
            secs = int(elapsed)
        else:
            secs = int((elapsed / n) * (lenx - n))
        mins, secs = divmod(secs, 60)
        hrs, mins = divmod(mins, 60)

        return "%02d:%02d:%02d" % (hrs, mins, secs)

    def format(starttime, n, lenx):
        out = plainformat(n, lenx) + " "
        if n == lenx:
            end = "     "
        else:
            end = " ETA "
        end += eta(time.time() - starttime, n, lenx)
        out += bars(WIDTH - len(out) - len(end), n, lenx)
        out += end
        return out

    starttime = time.time()
    lenx = len(x)
    for n, y in enumerate(x):
        sys.stderr.write("\r" + format(starttime, n, lenx))
        yield y
    sys.stderr.write("\r" + format(starttime, n + 1, lenx) + "\n")


class IterBetter:
    """
    Returns an object that can be used as an iterator
    but can also be used via __getitem__ (although it
    cannot go backwards -- that is, you cannot request
    `iterbetter[0]` after requesting `iterbetter[1]`).

        >>> import itertools
        >>> c = iterbetter(itertools.count())
        >>> c[1]
        1
        >>> c[5]
        5
        >>> c[3]
        Traceback (most recent call last):
            ...
        IndexError: already passed 3

    It is also possible to get the first value of the iterator or None.

        >>> c = iterbetter(iter([3, 4, 5]))
        >>> print(c.first())
        3
        >>> c = iterbetter(iter([]))
        >>> print(c.first())
        None

    For boolean test, IterBetter peeps at first value in the itertor without effecting the iteration.

        >>> c = iterbetter(iter(range(5)))
        >>> bool(c)
        True
        >>> list(c)
        [0, 1, 2, 3, 4]
        >>> c = iterbetter(iter([]))
        >>> bool(c)
        False
        >>> list(c)
        []
    """

    def __init__(self, iterator):
        self.i, self.c = iterator, 0

    def first(self, default=None):
        """Returns the first element of the iterator or None when there are no
        elements.

        If the optional argument default is specified, that is returned instead
        of None when there are no elements.
        """
        try:
            return next(iter(self))
        except StopIteration:
            return default

    def __iter__(self):
        if hasattr(self, "_head"):
            yield self._head

        while 1:
            try:
                yield next(self.i)
            except StopIteration:
                return
            self.c += 1

    def __getitem__(self, i):
        # todo: slices
        if i < self.c:
            raise IndexError("already passed " + str(i))
        try:
            while i > self.c:
                next(self.i)
                self.c += 1
            # now self.c == i
            self.c += 1
            return next(self.i)
        except StopIteration:
            raise IndexError(str(i))

    def __nonzero__(self):
        if hasattr(self, "__len__"):
            return self.__len__() != 0
        elif hasattr(self, "_head"):
            return True
        else:
            try:
                self._head = next(self.i)
            except StopIteration:
                return False
            else:
                return True

    __bool__ = __nonzero__


iterbetter = IterBetter


def safeiter(it, cleanup=None, ignore_errors=True):
    """Makes an iterator safe by ignoring the exceptions occurred during the iteration."""

    def next():
        while True:
            try:
                return next(it)
            except StopIteration:
                raise
            except:
                traceback.print_exc()

    it = iter(it)
    while True:
        yield next()


def safewrite(filename, content):
    """Writes the content to a temp file and then moves the temp file to
    given filename to avoid overwriting the existing file in case of errors.
    """
    with open(filename + ".tmp", "w") as f:
        f.write(content)
    shutil.move(f.name, filename)


def dictreverse(mapping):
    """
    Returns a new dictionary with keys and values swapped.

        >>> dictreverse({1: 2, 3: 4})
        {2: 1, 4: 3}
    """
    return {value: key for (key, value) in iteritems(mapping)}


def dictfind(dictionary, element):
    """
    Returns a key whose value in `dictionary` is `element`
    or, if none exists, None.

        >>> d = {1:2, 3:4}
        >>> dictfind(d, 4)
        3
        >>> dictfind(d, 5)
    """
    for key, value in iteritems(dictionary):
        if element is value:
            return key


def dictfindall(dictionary, element):
    """
    Returns the keys whose values in `dictionary` are `element`
    or, if none exists, [].

        >>> d = {1:4, 3:4}
        >>> dictfindall(d, 4)
        [1, 3]
        >>> dictfindall(d, 5)
        []
    """
    res = []
    for key, value in iteritems(dictionary):
        if element is value:
            res.append(key)
    return res


def dictincr(dictionary, element):
    """
    Increments `element` in `dictionary`,
    setting it to one if it doesn't exist.

        >>> d = {1:2, 3:4}
        >>> dictincr(d, 1)
        3
        >>> d[1]
        3
        >>> dictincr(d, 5)
        1
        >>> d[5]
        1
    """
    dictionary.setdefault(element, 0)
    dictionary[element] += 1
    return dictionary[element]


def dictadd(*dicts):
    """
    Returns a dictionary consisting of the keys in the argument dictionaries.
    If they share a key, the value from the last argument is used.

        >>> dictadd({1: 0, 2: 0}, {2: 1, 3: 1})
        {1: 0, 2: 1, 3: 1}
    """
    result = {}
    for dct in dicts:
        result.update(dct)
    return result


def requeue(queue, index=-1):
    """Returns the element at index after moving it to the beginning of the queue.

    >>> x = [1, 2, 3, 4]
    >>> requeue(x)
    4
    >>> x
    [4, 1, 2, 3]
    """
    x = queue.pop(index)
    queue.insert(0, x)
    return x


def restack(stack, index=0):
    """Returns the element at index after moving it to the top of stack.

    >>> x = [1, 2, 3, 4]
    >>> restack(x)
    1
    >>> x
    [2, 3, 4, 1]
    """
    x = stack.pop(index)
    stack.append(x)
    return x


def listget(lst, ind, default=None):
    """
    Returns `lst[ind]` if it exists, `default` otherwise.

        >>> listget(['a'], 0)
        'a'
        >>> listget(['a'], 1)
        >>> listget(['a'], 1, 'b')
        'b'
    """
    if len(lst) - 1 < ind:
        return default
    return lst[ind]


def intget(integer, default=None):
    """
    Returns `integer` as an int or `default` if it can't.

        >>> intget('3')
        3
        >>> intget('3a')
        >>> intget('3a', 0)
        0
    """
    try:
        return int(integer)
    except (TypeError, ValueError):
        return default


def datestr(then, now=None):
    """
    Converts a (UTC) datetime object to a nice string representation.

        >>> from datetime import datetime, timedelta
        >>> d = datetime(1970, 5, 1)
        >>> datestr(d, now=d)
        '0 microseconds ago'
        >>> for t, v in iteritems({
        ...   timedelta(microseconds=1): '1 microsecond ago',
        ...   timedelta(microseconds=2): '2 microseconds ago',
        ...   -timedelta(microseconds=1): '1 microsecond from now',
        ...   -timedelta(microseconds=2): '2 microseconds from now',
        ...   timedelta(microseconds=2000): '2 milliseconds ago',
        ...   timedelta(seconds=2): '2 seconds ago',
        ...   timedelta(seconds=2*60): '2 minutes ago',
        ...   timedelta(seconds=2*60*60): '2 hours ago',
        ...   timedelta(days=2): '2 days ago',
        ... }):
        ...     assert datestr(d, now=d+t) == v
        >>> datestr(datetime(1970, 1, 1), now=d)
        'January  1'
        >>> datestr(datetime(1969, 1, 1), now=d)
        'January  1, 1969'
        >>> datestr(datetime(1970, 6, 1), now=d)
        'June  1, 1970'
        >>> datestr(None)
        ''
    """

    def agohence(n, what, divisor=None):
        if divisor:
            n = n // divisor

        out = str(abs(n)) + " " + what  # '2 days'
        if abs(n) != 1:
            out += "s"  # '2 days'

        out += " "  # '2 days '
        if n < 0:
            out += "from now"
        else:
            out += "ago"
        return out  # '2 days ago'

    oneday = 24 * 60 * 60

    if not then:
        return ""

    if not now:
        now = datetime.datetime.utcnow()

    if type(now).__name__ == "DateTime":
        now = datetime.datetime.fromtimestamp(now)

    if type(then).__name__ == "DateTime":
        then = datetime.datetime.fromtimestamp(then)
    elif type(then).__name__ == "date":
        then = datetime.datetime(then.year, then.month, then.day)

    delta = now - then
    deltaseconds = int(delta.days * oneday + delta.seconds + delta.microseconds * 1e-06)
    deltadays = abs(deltaseconds) // oneday
    if deltaseconds < 0:
        deltadays *= -1  # fix for oddity of floor

    if deltadays:
        if abs(deltadays) < 4:
            return agohence(deltadays, "day")

        # Trick to display 'June 3' instead of 'June 03'
        # Even though the %e format in strftime does that, it doesn't work on Windows.
        out = then.strftime("%B %d").replace(" 0", "  ")

        if then.year != now.year or deltadays < 0:
            out += ", %s" % then.year
        return out

    if int(deltaseconds):
        if abs(deltaseconds) > (60 * 60):
            return agohence(deltaseconds, "hour", 60 * 60)
        elif abs(deltaseconds) > 60:
            return agohence(deltaseconds, "minute", 60)
        else:
            return agohence(deltaseconds, "second")

    deltamicroseconds = delta.microseconds
    if delta.days:
        deltamicroseconds = int(delta.microseconds - 1e6)  # datetime oddity

    if abs(deltamicroseconds) > 1000:
        return agohence(deltamicroseconds, "millisecond", 1000)

    return agohence(deltamicroseconds, "microsecond")


def numify(string):
    """
    Removes all non-digit characters from `string`.

        >>> numify('800-555-1212')
        '8005551212'
        >>> numify('800.555.1212')
        '8005551212'

    """
    return "".join([c for c in str(string) if c.isdigit()])


def denumify(string, pattern):
    """
    Formats `string` according to `pattern`, where the letter X gets replaced
    by characters from `string`.

        >>> denumify("8005551212", "(XXX) XXX-XXXX")
        '(800) 555-1212'

    """
    out = []
    for c in pattern:
        if c == "X":
            out.append(string[0])
            string = string[1:]
        else:
            out.append(c)
    return "".join(out)


def commify(n):
    """
    Add commas to an integer `n`.

        >>> commify(1)
        '1'
        >>> commify(123)
        '123'
        >>> commify(-123)
        '-123'
        >>> commify(1234)
        '1,234'
        >>> commify(1234567890)
        '1,234,567,890'
        >>> commify(123.0)
        '123.0'
        >>> commify(1234.5)
        '1,234.5'
        >>> commify(1234.56789)
        '1,234.56789'
        >>> commify(' %.2f ' % -1234.5)
        '-1,234.50'
        >>> commify(None)
        >>>

    """
    if n is None:
        return None

    n = str(n).strip()

    if n.startswith("-"):
        prefix = "-"
        n = n[1:].strip()
    else:
        prefix = ""

    if "." in n:
        dollars, cents = n.split(".")
    else:
        dollars, cents = n, None

    r = []
    for i, c in enumerate(str(dollars)[::-1]):
        if i and (not (i % 3)):
            r.insert(0, ",")
        r.insert(0, c)
    out = "".join(r)
    if cents:
        out += "." + cents
    return prefix + out


def dateify(datestring):
    """
    Formats a numified `datestring` properly.
    """
    return denumify(datestring, "XXXX-XX-XX XX:XX:XX")


def nthstr(n):
    """
    Formats an ordinal.
    Doesn't handle negative numbers.

        >>> nthstr(1)
        '1st'
        >>> nthstr(0)
        '0th'
        >>> [nthstr(x) for x in [2, 3, 4, 5, 10, 11, 12, 13, 14, 15]]
        ['2nd', '3rd', '4th', '5th', '10th', '11th', '12th', '13th', '14th', '15th']
        >>> [nthstr(x) for x in [91, 92, 93, 94, 99, 100, 101, 102]]
        ['91st', '92nd', '93rd', '94th', '99th', '100th', '101st', '102nd']
        >>> [nthstr(x) for x in [111, 112, 113, 114, 115]]
        ['111th', '112th', '113th', '114th', '115th']

    """

    assert n >= 0
    if n % 100 in [11, 12, 13]:
        return "%sth" % n
    return {1: "%sst", 2: "%snd", 3: "%srd"}.get(n % 10, "%sth") % n


def cond(predicate, consequence, alternative=None):
    """
    Function replacement for if-else to use in expressions.

        >>> x = 2
        >>> cond(x % 2 == 0, "even", "odd")
        'even'
        >>> cond(x % 2 == 0, "even", "odd") + '_row'
        'even_row'
    """
    if predicate:
        return consequence
    else:
        return alternative


class CaptureStdout:
    """
    Captures everything `func` prints to stdout and returns it instead.

        >>> def idiot():
        ...     print("foo")
        >>> capturestdout(idiot)()
        'foo\\n'

    **WARNING:** Not threadsafe!
    """

    def __init__(self, func):
        self.func = func

    def __call__(self, *args, **keywords):
        out = StringIO()
        oldstdout = sys.stdout
        sys.stdout = out
        try:
            self.func(*args, **keywords)
        finally:
            sys.stdout = oldstdout
        return out.getvalue()


capturestdout = CaptureStdout


class Profile:
    """
    Profiles `func` and returns a tuple containing its output
    and a string with human-readable profiling information.

        >>> import time
        >>> out, inf = profile(time.sleep)(.001)
        >>> out
        >>> inf[:10].strip()
        'took 0.0'
    """

    def __init__(self, func):
        self.func = func

    def __call__(self, *args):  # , **kw):   kw unused
        import cProfile
        import os
        import pstats
        import tempfile

        f, filename = tempfile.mkstemp()
        os.close(f)

        prof = cProfile.Profile()

        stime = time.time()
        result = prof.runcall(self.func, *args)
        stime = time.time() - stime

        out = StringIO()
        stats = pstats.Stats(prof, stream=out)
        stats.strip_dirs()
        stats.sort_stats("time", "calls")
        stats.print_stats(40)
        stats.print_callers()

        x = "\n\ntook " + str(stime) + " seconds\n"
        x += out.getvalue()

        # remove the tempfile
        try:
            os.remove(filename)
        except OSError:
            pass

        return result, x


profile = Profile


def tryall(context, prefix=None):
    """
    Tries a series of functions and prints their results.
    `context` is a dictionary mapping names to values;
    the value will only be tried if it's callable.

        >>> tryall(dict(j=lambda: True))
        j: True
        ----------------------------------------
        results:
           True: 1

    For example, you might have a file `test/stuff.py`
    with a series of functions testing various things in it.
    At the bottom, have a line:

        if __name__ == "__main__": tryall(globals())

    Then you can run `python test/stuff.py` and get the results of
    all the tests.
    """
    context = context.copy()  # vars() would update
    results = {}
    for key, value in iteritems(context):
        if not hasattr(value, "__call__"):
            continue
        if prefix and not key.startswith(prefix):
            continue
        print(key + ":", end=" ")
        try:
            r = value()
            dictincr(results, r)
            print(r)
        except:
            print("ERROR")
            dictincr(results, "ERROR")
            print("   " + "\n   ".join(traceback.format_exc().split("\n")))

    print("-" * 40)
    print("results:")
    for key, value in iteritems(results):
        print(" " * 2, str(key) + ":", value)


class ThreadedDict(threadlocal):
    """
    【功能层】线程本地存储容器，不同线程通过同一个 ThreadedDict 实例访问各自独立的数据。
    【设计层】继承 threading.local，Python 的线程本地变量原生机制；
             同时实现完整的 dict 接口（__getitem__、__setitem__ 等），
             让线程本地数据能像字典一样操作。
             _instances 类变量使用 set 追踪所有实例，支持 clear_all() 批量清理，
             是"注册表模式"（Registry Pattern）的简单应用。
    【上下文层】web.ctx（请求上下文）的底层存储即为 ThreadedDict 实例。
             每个请求在独立线程（或协程）中处理，ctx 的读写互不干扰，
             这是 web.py 实现线程安全请求隔离的核心机制。

    Thread local storage.

        >>> d = ThreadedDict()
        >>> d.x = 1
        >>> d.x
        1
        >>> import threading
        >>> def f(): d.x = 2
        ...
        >>> t = threading.Thread(target=f)
        >>> t.start()
        >>> t.join()
        >>> d.x
        1
    """

    _instances = set()  # 类级注册表，追踪所有 ThreadedDict 实例，用于 clear_all

    def __init__(self):
        ThreadedDict._instances.add(self)  # 创建时注册自身

    def __del__(self):
        ThreadedDict._instances.remove(self)  # 销毁时注销，防止内存泄漏

    def __hash__(self):
        return id(self)  # 使实例可放入 set，以对象地址为哈希值

    def clear_all():
        """【功能层】清除所有 ThreadedDict 实例中当前线程的数据。
        【上下文层】application._cleanup() 在每次请求结束后调用此方法，
                  防止线程复用时上一请求的数据污染下一请求。"""
        for t in list(ThreadedDict._instances):
            t.clear()

    clear_all = staticmethod(clear_all)  # 定义为静态方法，无需实例即可调用

    # Define all these methods to more or less fully emulate dict -- attribute access
    # is built into threading.local.

    def __getitem__(self, key):
        return self.__dict__[key]

    def __setitem__(self, key, value):
        self.__dict__[key] = value

    def __delitem__(self, key):
        del self.__dict__[key]

    def __contains__(self, key):
        return key in self.__dict__

    has_key = __contains__

    def clear(self):
        self.__dict__.clear()

    def copy(self):
        return self.__dict__.copy()

    def get(self, key, default=None):
        return self.__dict__.get(key, default)

    def items(self):
        return self.__dict__.items()

    def iteritems(self):
        return iteritems(self.__dict__)

    def keys(self):
        return self.__dict__.keys()

    def iterkeys(self):
        try:
            return iterkeys(self.__dict__)
        except NameError:
            return self.__dict__.keys()

    iter = iterkeys

    def values(self):
        return self.__dict__.values()

    def itervalues(self):
        return itervalues(self.__dict__)

    def pop(self, key, *args):
        return self.__dict__.pop(key, *args)

    def popitem(self):
        return self.__dict__.popitem()

    def setdefault(self, key, default=None):
        return self.__dict__.setdefault(key, default)

    def update(self, *args, **kwargs):
        self.__dict__.update(*args, **kwargs)

    def __repr__(self):
        return "<ThreadedDict %r>" % self.__dict__

    __str__ = __repr__


threadeddict = ThreadedDict


def autoassign(self, locals):
    """
    Automatically assigns local variables to `self`.

        >>> self = storage()
        >>> autoassign(self, dict(a=1, b=2))
        >>> self.a
        1
        >>> self.b
        2

    Generally used in `__init__` methods, as in:

        def __init__(self, foo, bar, baz=1): autoassign(self, locals())
    """
    for key, value in iteritems(locals):
        if key == "self":
            continue
        setattr(self, key, value)


def to36(q):
    """
    Converts an integer to base 36 (a useful scheme for human-sayable IDs).

        >>> to36(35)
        'z'
        >>> to36(119292)
        '2k1o'
        >>> int(to36(939387374), 36)
        939387374
        >>> to36(0)
        '0'
        >>> to36(-393)
        Traceback (most recent call last):
            ...
        ValueError: must supply a positive integer

    """
    if q < 0:
        raise ValueError("must supply a positive integer")

    letters = "0123456789abcdefghijklmnopqrstuvwxyz"
    converted = []
    while q != 0:
        q, r = divmod(q, 36)
        converted.insert(0, letters[r])
    return "".join(converted) or "0"


r_url = re_compile(r"(?<!\()(http://(\S+))")


def sendmail(from_address, to_address, subject, message, headers=None, **kw):
    """
    Sends the email message `message` with mail and envelope headers
    for from `from_address_` to `to_address` with `subject`.
    Additional email headers can be specified with the dictionary
    `headers.

    Optionally cc, bcc and attachments can be specified as keyword arguments.
    Attachments must be an iterable and each attachment can be either a
    filename or a file object or a dictionary with filename, content and
    optionally content_type keys.

    If `web.config.smtp_server` is set, it will send the message
    to that SMTP server. Otherwise it will look for
    `/usr/sbin/sendmail`, the typical location for the sendmail-style
    binary. To use sendmail from a different path, set `web.config.sendmail_path`.
    """
    attachments = kw.pop("attachments", [])
    mail = _EmailMessage(from_address, to_address, subject, message, headers, **kw)

    for a in attachments:
        if isinstance(a, dict):
            mail.attach(a["filename"], a["content"], a.get("content_type"))
        elif hasattr(a, "read"):  # file
            filename = os.path.basename(getattr(a, "name", ""))
            content_type = getattr(a, "content_type", None)
            mail.attach(filename, a.read(), content_type)
        elif isinstance(a, str):
            f = open(a, "rb")
            content = f.read()
            f.close()
            filename = os.path.basename(a)
            mail.attach(filename, content, None)
        else:
            raise ValueError("Invalid attachment: %s" % repr(a))

    mail.send()


class _EmailMessage:
    def __init__(self, from_address, to_address, subject, message, headers=None, **kw):
        def listify(x):
            if not isinstance(x, list):
                return [safestr(x)]
            else:
                return [safestr(a) for a in x]

        subject = safestr(subject)
        message = safestr(message)

        from_address = safestr(from_address)
        to_address = listify(to_address)
        cc = listify(kw.get("cc", []))
        bcc = listify(kw.get("bcc", []))
        recipients = to_address + cc + bcc

        import email.utils

        self.from_address = email.utils.parseaddr(from_address)[1]
        self.recipients = [email.utils.parseaddr(r)[1] for r in recipients]

        self.headers = dictadd(
            {"From": from_address, "To": ", ".join(to_address), "Subject": subject},
            headers or {},
        )

        if cc:
            self.headers["Cc"] = ", ".join(cc)

        self.message = self.new_message()
        self.message.add_header("Content-Transfer-Encoding", "7bit")
        self.message.add_header("Content-Disposition", "inline")
        self.message.add_header("MIME-Version", "1.0")
        self.message.set_payload(message, "utf-8")
        self.multipart = False

    def new_message(self):
        from email.message import Message

        return Message()

    def attach(self, filename, content, content_type=None):
        if not self.multipart:
            msg = self.new_message()
            msg.add_header("Content-Type", "multipart/mixed")
            msg.attach(self.message)
            self.message = msg
            self.multipart = True

        import mimetypes

        try:
            from email import encoders
        except:
            from email import Encoders as encoders

        content_type = (
            content_type
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )

        msg = self.new_message()
        msg.set_payload(content)
        msg.add_header("Content-Type", content_type)
        msg.add_header("Content-Disposition", "attachment", filename=filename)

        if not content_type.startswith("text/"):
            encoders.encode_base64(msg)

        self.message.attach(msg)

    def prepare_message(self):
        for k, v in iteritems(self.headers):
            if k.lower() == "content-type":
                self.message.set_type(v)
            else:
                self.message.add_header(k, v)

        self.headers = {}

    def send(self):
        self.prepare_message()
        message_text = self.message.as_string()

        try:
            from . import webapi
        except ImportError:
            webapi = Storage(config=Storage())

        if webapi.config.get("smtp_server"):
            self.send_with_smtp(message_text)
        elif webapi.config.get("email_engine") == "aws":
            self.send_with_aws(message_text)
        else:
            self.default_email_sender(message_text)

    def send_with_aws(self, message_text):
        try:
            from . import webapi
        except ImportError:
            webapi = Storage(config=Storage())

        import boto.ses

        c = boto.ses.SESConnection(
            aws_access_key_id=webapi.config.get("aws_access_key_id"),
            aws_secret_access_key=webapi.config.get("aws_secret_access_key"),
        )
        c.send_raw_email(message_text, self.from_address, self.recipients)

    def send_with_smtp(self, message_text):
        try:
            from . import webapi
        except ImportError:
            webapi = Storage(config=Storage())

        server = webapi.config.get("smtp_server")
        port = webapi.config.get("smtp_port", 0)
        username = webapi.config.get("smtp_username")
        password = webapi.config.get("smtp_password")
        debug_level = webapi.config.get("smtp_debuglevel", None)
        starttls = webapi.config.get("smtp_starttls", False)

        import smtplib

        smtpserver = smtplib.SMTP(server, port)

        if debug_level:
            smtpserver.set_debuglevel(debug_level)

        if starttls:
            smtpserver.ehlo()
            smtpserver.starttls()
            smtpserver.ehlo()

        if username and password:
            smtpserver.login(username, password)

        smtpserver.sendmail(self.from_address, self.recipients, message_text)
        smtpserver.quit()

    def default_email_sender(self, message_text):
        try:
            from . import webapi
        except ImportError:
            webapi = Storage(config=Storage())

        sendmail = webapi.config.get("sendmail_path", "/usr/sbin/sendmail")

        assert not self.from_address.startswith("-"), "security"

        for r in self.recipients:
            assert not r.startswith("-"), "security"

        cmd = [sendmail, "-f", self.from_address] + self.recipients

        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        p.stdin.write(message_text.encode("utf-8"))
        p.stdin.close()
        p.wait()

    def __repr__(self):
        return "<EmailMessage>"

    def __str__(self):
        return self.message.as_string()


if __name__ == "__main__":
    import doctest

    doctest.testmod()
