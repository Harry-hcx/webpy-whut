# web.py 源码剖析报告

**课程**：Python 高级程序设计  
**项目**：web.py —— 轻量级 Python Web 框架源码复现与注释  
**仓库**：本仓库为 [webpy/webpy](https://github.com/webpy/webpy)（GitHub Stars > 8000）的 Fork，版本 0.76  
**日期**：2026 年 6 月
**班级学号**：软件2301胡晨煊 1023000334

**仓库名称**：[Harry-hcx/webpy-whut: 武汉理工大学Python程序设计大作业](https://github.com/Harry-hcx/webpy-whut)

https://github.com/Harry-hcx/webpy-whut

---

## 一、项目选择与背景

### 1.1 为什么选 web.py

web.py 由互联网著名活动家 Aaron Swartz 于 2006 年创作，以极简著称：整个框架核心代码约 **7000+ 有效行**，却实现了一个生产可用的 Web 框架所需的全部基础设施——路由分发、模板引擎、数据库抽象、会话管理、HTTP 工具和 WSGI 适配。

选择理由：
- GitHub Stars > 8000，是真实工业项目；
- 代码量适中（核心 7 个模块，总计约 7200 行），满足"有效代码行数 2000 行"要求；
- 密集使用 Python 高级特性：元类、装饰器、上下文管理器、生成器、线程本地变量、动态类创建等；
- 架构清晰，模块间职责分明，适合逐层剖析。

### 1.2 项目概况

| 模块文件 | 总行数 | 主要职责 |
|---|---|---|
| `web/application.py` | 813 | WSGI 应用核心、路由分发、中间件管道 |
| `web/utils.py` | 1617 | 工具函数集（Storage、Memoize、ThreadedDict 等） |
| `web/webapi.py` | 655 | HTTP API（ctx 上下文、HTTPError 体系、input 解析） |
| `web/session.py` | 456 | 会话管理（多种存储后端） |
| `web/template.py` | 1755 | 内置模板引擎（编译型，Python 内嵌语法） |
| `web/db.py` | 1750 | 数据库抽象层（SQLite/MySQL/PostgreSQL） |
| `web/http.py` | 165 | HTTP 工具（缓存控制、URL 构建） |
| `web/net.py` | ~200 | 网络工具（HTML 转义、HTTP 日期解析） |

---

## 二、环境搭建与运行验证

### 2.1 依赖安装

```bash
# 克隆本仓库
git clone <repo_url>
cd webpy-whut

# 安装依赖（Python 3.10+）
pip install -r requirements.txt
# 核心依赖：
#   cheroot>=6.0.0     —— 高性能 WSGI HTTP 服务器
#   more_itertools>=2.6 —— 模板解析用的 peekable 迭代器
#   multipart>=0.2.4   —— 文件上传/多部分表单解析
```

### 2.2 最小示例运行

创建 `hello.py`：

```python
import web

urls = ('/hello', 'hello', '/.*', 'index')

class hello:
    def GET(self):
        return "Hello, web.py!"

class index:
    def GET(self):
        raise web.seeother('/hello')

app = web.application(urls, globals())

if __name__ == '__main__':
    app.run()
```

```bash
python hello.py
# 输出：http://0.0.0.0:8080/
```

访问 `http://localhost:8080/hello` 返回 `Hello, web.py!`，访问 `/` 自动跳转。

### 2.3 运行测试套件

```bash
pip install -r test_requirements.txt
python -m pytest tests/ -v
```

测试覆盖 `test_application.py`、`test_template.py`、`test_session.py`、`test_db.py` 等 10 个测试文件，全部通过。

---

## 三、架构总览

### 3.1 模块依赖关系

```
用户代码
    │
    ▼
web/__init__.py  ← 门面层（Facade），统一导出所有 API
    │
    ├── application.py  ← 请求处理主干
    │       ├── 路由匹配  (_match)
    │       ├── 处理器链  (handle_with_processors)
    │       └── WSGI 适配 (wsgifunc)
    │
    ├── webapi.py       ← HTTP 语义层
    │       ├── ctx (ThreadedDict)  ← 请求上下文
    │       ├── HTTPError 体系       ← 响应控制
    │       └── input/cookies        ← 输入解析
    │
    ├── utils.py        ← 基础设施
    │       ├── Storage              ← 可属性访问的字典
    │       ├── ThreadedDict         ← 线程本地存储
    │       └── Memoize              ← 函数缓存
    │
    ├── template.py     ← 模板引擎
    │       ├── Parser  → AST 节点树
    │       ├── Template → Python 代码 → exec
    │       └── Render  → 目录级模板管理
    │
    ├── session.py      ← 会话管理
    │       ├── Session  ← 管理器（注入处理器链）
    │       └── Store 体系（Disk/DB/Memory）
    │
    ├── db.py           ← 数据库抽象
    │       ├── SQLQuery/SQLParam  ← 类型安全 SQL 构建
    │       └── DB + 子类          ← 统一操作接口
    │
    └── http.py         ← HTTP 工具（缓存/URL）
```

### 3.2 请求生命周期

```
HTTP 请求
    │
    ▼
wsgifunc(env, start_response)
    │
    ├─ _cleanup()          清理上次请求的线程本地数据
    ├─ load(env)           初始化 ctx（path/method/ip/headers...）
    │
    ▼
handle_with_processors()
    │
    ├─ processor[0]: loadhook(_load)      压入 app_stack
    ├─ processor[1]: loadhook(Reloader)   热重载检测
    ├─ processor[2]: Session._processor  加载会话
    │       ...（用户自定义处理器）
    │
    ▼
handle()
    │
    ├─ _match(mapping, ctx.path)   正则匹配路由
    └─ _delegate(handler, args)    实例化处理器类，调用 GET/POST 等
            │
            ▼
        return 响应字符串 / 抛出 HTTPError
    │
    ▼
build_result()     str → bytes
start_response()   发送状态行和响应头
yield bytes        流式返回响应体
    │
    ▼
cleanup()          清理线程本地数据（chain 末尾）
```

---

## 四、Python 高级特性剖析

### 4.1 元类（Metaclass）—— `auto_application`

```python
# web/application.py
class auto_application(application):
    def __init__(self):
        application.__init__(self)

        class metapage(type):
            def __init__(klass, name, bases, attrs):
                type.__init__(klass, name, bases, attrs)
                path = attrs.get("path", "/" + name)
                if path is not None:
                    self.add_mapping(path, klass)  # 自动注册路由

        @with_metaclass(metapage)
        class page:
            path = None

        self.page = page
```

**剖析**：`metapage` 继承 `type`，重载 `__init__`。每当用户定义 `class foo(app.page):`，Python 的类创建机制自动调用 `metapage.__init__`，在路由表中注册该类。这是元类最经典的用途——"在类定义时执行副作用"，消除了手动维护 URL 映射表的需要。

`_status_code()` 函数是另一处元编程：

```python
return type(classname, (HTTPError, object), {"__doc__": docstring, "__init__": __init__})
```

用 `type(name, bases, dict)` 在运行时动态创建 HTTP 状态码类（OK、NotFound 等），避免为每个状态码重复定义类。

---

### 4.2 装饰器（Decorator）—— `timelimit` 和 `loadhook/unloadhook`

```python
# web/utils.py —— 装饰器工厂（三层嵌套）
def timelimit(timeout):
    def _1(function):
        def _2(*args, **kw):
            class Dispatch(threading.Thread):
                def run(self):
                    try:
                        self.result = function(*args, **kw)
                    except:
                        self.error = sys.exc_info()
            c = Dispatch()
            c.join(timeout)
            if c.is_alive():
                raise RuntimeError("took too long")
            return c.result
        return _2
    return _1
```

**剖析**：三层闭包的装饰器工厂：`timelimit(n)` 返回装饰器 `_1`，`_1(func)` 返回包装函数 `_2`。`_2` 每次调用时在独立守护线程中执行原函数，主线程最多等待 `timeout` 秒，超时则抛出异常。内部类 `Dispatch` 的 `daemon=True` 保证主进程退出时不被阻塞。

```python
# web/application.py —— 高阶函数构建处理器
def loadhook(h):
    def processor(handler):
        h()              # 前置钩子
        return handler() # 继续处理链
    return processor

def unloadhook(h):
    def processor(handler):
        try:
            result = handler()
        except:
            h()   # 异常时也执行后置钩子
            raise
        # 生成器响应的特殊处理（流式输出场景）
        if result and hasattr(result, "__next__"):
            return wrap(result)
        else:
            h()
            return result
    ...
    return processor
```

**剖析**：`loadhook`/`unloadhook` 是高阶函数（接受函数、返回函数），将普通的"前置/后置操作"包装成符合处理器协议的中间件，实现 AOP（面向切面）风格的请求钩子。`unloadhook` 对生成器响应的处理体现了 Python 生成器与异常处理的深度结合。

---

### 4.3 生成器（Generator）—— `wsgifunc` 与响应流

```python
# web/application.py
def build_result(result):
    for r in result:
        if isinstance(r, bytes):
            yield r
        else:
            yield str(r).encode("utf-8")

def cleanup():
    self._cleanup()
    yield b""  # 必须 yield 使其成为生成器

return itertools.chain(result, cleanup())
```

**剖析**：WSGI 规范要求响应体是可迭代对象。`build_result` 用生成器惰性地将响应转为 `bytes`；`cleanup()` 是一个仅含 `yield b""` 的生成器，被追加到 `itertools.chain` 末尾，保证"响应体全部发送完毕后才执行线程清理"——这是生成器控制执行时序的精妙用法，绕过了 try/finally 无法横跨生成器边界的限制。

`group()` 是另一处生成器应用：

```python
def group(seq, size):
    return (seq[i : i + size] for i in range(0, len(seq), size))
```

用生成器表达式惰性地分组序列，将路由平铺元组 `(url1, cls1, url2, cls2, ...)` 转为 `[(url1, cls1), ...]`，内存占用为 O(1)。

---

### 4.4 上下文管理器（Context Manager）—— 数据库事务

```python
# web/db.py
class TransactionContext:
    def __enter__(self):
        self.transaction = self.db.transaction()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.transaction.rollback()
        else:
            self.transaction.commit()
        return False  # 不吞掉异常
```

**剖析**：实现 `__enter__`/`__exit__` 协议，支持 `with db.transaction():` 语法。无论代码块是否抛出异常，`__exit__` 都会被调用：有异常则回滚（`rollback`），无异常则提交（`commit`）。这是上下文管理器最典型的应用——资源的确定性释放与事务的原子性保证。

---

### 4.5 线程本地变量（Thread-Local Storage）—— `ThreadedDict` 与 `ctx`

```python
# web/utils.py
from threading import local as threadlocal

class ThreadedDict(threadlocal):
    _instances = set()  # 注册表，追踪所有实例

    def clear_all():
        for t in list(ThreadedDict._instances):
            t.clear()
    clear_all = staticmethod(clear_all)

    # 完整实现 dict 接口...
```

```python
# web/webapi.py
ctx = context = threadeddict()
```

**剖析**：`ThreadedDict` 继承 `threading.local`，每个线程通过同一个 `ctx` 对象访问各自独立的数据——这是 web.py 实现"无需显式传递请求对象"的核心机制。`_instances` 注册表使 `clear_all()` 能批量清理所有实例当前线程的数据，防止线程池复用时数据泄漏。

---

### 4.6 Memoize —— 缓存与并发安全

```python
# web/utils.py
class Memoize:
    def __call__(self, *args, **keywords):
        key = (args, tuple(keywords.items()))
        with self.running_lock:
            if not self.running.get(key):
                self.running[key] = threading.Lock()

        def update(block=False):
            if self.running[key].acquire(block):
                try:
                    self.cache[key] = (self.func(*args, **keywords), time.time())
                finally:
                    self.running[key].release()

        if key not in self.cache:
            update(block=True)          # 首次：同步阻塞
        elif ...:
            if self.background:
                threading.Thread(target=update).start()  # 后台刷新
            else:
                update()
        return self.cache[key][0]

re_compile = memoize(re.compile)  # 缓存正则编译
```

**剖析**：两级锁：`running_lock` 保护 `running` 字典本身的并发修改；`running[key]` 是每个缓存槽独立的锁，防止多线程同时计算同一 key。`background=True` 时异步刷新：旧值立即返回，新值在后台更新，避免缓存过期导致的请求延迟峰值（"缓存雪崩"的软化处理）。

---

### 4.7 模板引擎 —— 编译型设计

```
模板文本  ──Parser──►  AST节点树  ──emit()──►  Python代码字符串  ──exec()──►  __template__ 函数
                        │                              │
                     TextNode                    safecheck()
                     ExpressionNode              （AST安全校验）
                     ForNode
                     IfNode
                     ...
```

**剖析**：web.py 的模板引擎是"编译型"而非"解释型"：模板首次加载时编译为 Python 函数，后续渲染直接调用函数，性能远优于逐行解释。`BaseTemplate.make_env()` 构造沙箱环境，只暴露 `TEMPLATE_BUILTINS`（有限的安全内置函数），通过 AST 遍历（`SafeVisitor`）拦截危险属性访问（如 `.__class__`、`.__dict__` 等），实现模板沙箱安全。

---

### 4.8 Storage —— 魔术方法重载

```python
# web/utils.py
class Storage(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as k:
            raise AttributeError(k)

    def __setattr__(self, key, value):
        self[key] = value

    def __delattr__(self, key):
        try:
            del self[key]
        except KeyError as k:
            raise AttributeError(k)
```

**剖析**：通过重载 `__getattr__`/`__setattr__`/`__delattr__` 将属性操作代理到字典操作，实现 `obj.key` 与 `obj['key']` 完全等价。`__getattr__` 只在常规属性查找失败时调用，因此实例方法（`update`、`items` 等继承自 dict）不受影响。这是 Python 描述符协议的实用应用，贯穿 web.py 所有数据容器（`ctx`、`config`、数据库行、会话数据）。

---

## 五、设计模式总结

| 设计模式 | 实现位置 | 说明 |
|---|---|---|
| 门面模式（Facade） | `web/__init__.py` | 统一导出所有子模块 API |
| 责任链（Chain of Responsibility） | `application.py` 处理器链 | processor 递归嵌套调用 |
| 策略模式（Strategy） | `session.py` Store 体系 | 存储后端可互换 |
| 模板方法（Template Method） | `db.py` DB 基类 | `select`/`insert` 等方法骨架固定，子类实现连接细节 |
| 注册表模式（Registry） | `utils.ThreadedDict._instances` | 追踪所有实例支持批量清理 |
| 值对象（Value Object） | `db.SQLParam` | 不可变，由值决定相等性 |
| 惰性初始化（Lazy Init） | `db.DB.ctx` property | 首次访问时才建立数据库连接 |

---

## 六、代码规范调整

在注释过程中发现并记录了以下规范问题（已在注释中说明，未修改逻辑）：

1. **`application.__init__` 的 `fvars={}` 默认参数**：Python 中可变默认参数是反模式（所有调用共享同一个字典实例）。web.py 此处因 `fvars` 只读不写，实际无害，但不符合最佳实践，应改为 `fvars=None`。

2. **`session.py` 的 `secret_key` 硬编码默认值**：`"fLjUfxqXtfNoIldA0A0J"` 是公开的默认密钥，生产环境若不修改存在安全风险，注释中已提示。

3. **`HTTPError.__init__` 的 `headers={}` 默认参数**：同上，可变默认参数问题，虽然框架内部每次都传入新字典，但接口设计上存在隐患。

4. **`safeiter` 中的名称遮蔽**：内部函数与 `builtins.next` 同名，已在注释中说明。

---

## 七、总结

web.py 是一个高度浓缩的工程样本，在约 7000 行代码中完整展示了：

- **元编程**：`type()` 动态类创建、元类自动注册路由；
- **函数式风格**：高阶函数构建中间件管道、闭包捕获上下文、生成器惰性求值；
- **并发安全**：`threading.local` 线程隔离、双级锁的 Memoize、守护线程超时控制；
- **协议实现**：`__getattr__`/`__setattr__`/`__call__`/`__enter__`/`__exit__`/`__iter__` 等魔术方法的全面应用；
- **编译型模板**：词法分析 → AST → 代码生成 → exec 沙箱执行的完整编译管道；
- **类型安全 SQL**：SQLParam/SQLQuery 从数据结构层面消除注入风险。

整个代码库"以最小的抽象做最多的事"，是 Python 高级特性工程化应用的优秀范本。