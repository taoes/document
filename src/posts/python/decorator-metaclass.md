---
title: "装饰器、描述符与元类：Python 元编程深度实践"
date: 2024-12-14
category:
  - Python
tag:
  - 元编程
  - 装饰器
  - 元类
icon: terminal
---

# 装饰器、描述符与元类：Python 元编程深度实践

元编程（Metaprogramming）是"编写操作程序的程序"。对很多 Python 工程师而言，装饰器、描述符与元类似乎是三个彼此孤立的知识点，散落在框架源码中难以拼出全貌。实际上，它们共享同一条底层主线：**Python 的一切都是对象，而对象的创建、属性访问与函数调用都是可被拦截的协议**。

本文不会重复"装饰器就是语法糖"这类入门结论，而是从真实框架源码出发，把这三者串成一条从"包装行为"到"重塑对象模型"的进阶路径，并给出生产环境中的坑与排查思路。

## 1. 从函数装饰器到类装饰器：行为包装的边界

装饰器的本质是对可调用对象的一等公民特性的利用。一个最朴素的函数装饰器等价于一次变量重绑定：

```python
def log(func):
    def wrapper(*args, **kwargs):
        print(f"call {func.__name__}")
        return func(*args, **kwargs)
    return wrapper

@log
def add(a, b):
    return a + b

# 等价于 add = log(add)
assert add(1, 2) == 3
```

这段代码看似简单，却埋着两个生产环境的经典坑。

**坑一：`functools.wraps` 缺失导致元数据丢失。** 装饰器返回的 `wrapper` 覆盖了原函数的 `__name__`、`__doc__`、`__module__` 等属性。一旦上层框架依赖函数名做路由（例如 Flask 的 URL 规则用 `view_func.__name__` 生成默认端点名），两个被同一装饰器包装的视图函数会得到相同的 `wrapper` 名，直接触发 `AssertionError: View function mapping is overwriting an existing endpoint function`。

**坑二：装饰器求值时机与导入副作用。** 装饰器在模块导入阶段即执行，而非调用阶段。若装饰器内部包含 I/O、连接数据库或读取配置，会在 `import` 时提前触发，破坏惰性加载。正确做法是让装饰器只返回闭包或对象，把重逻辑推迟到调用期：

```python
import functools

def traced(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        return result
    return wrapper
```

更进一步，当装饰器本身需要参数时，就出现了"装饰器工厂"：`@retry(times=3)` 中的 `retry(times=3)` 先执行并返回真正的装饰器。理解"工厂 → 装饰器 → 被包装对象"三层结构，是读懂 `click.option`、`pytest.fixture` 这类 API 的关键。

类装饰器则把"包装"的维度从"一次调用"提升到"整个类的生命周期"。它在类定义完成、绑定到名字之后立即被调用，接收类对象并返回新的类对象。典型用途是**注册**而非改造——这正是标准库 `dataclasses.dataclass` 与 `enum` 之外的注册表模式：

```python
registry = {}

def register(cls):
    registry[cls.__name__] = cls
    return cls

@register
class HandlerA:
    pass

assert "HandlerA" in registry
```

类装饰器与元类在"注册"这一需求上功能重叠，但前者更轻、可组合、可叠加多个；元类则深入类的创建过程本身。选型原则是：**能不改写类的继承与属性解析规则，就优先用类装饰器。**

## 2. 描述符协议：属性访问的拦截点

描述符（Descriptor）是实现 `__get__`、`__set__` 或 `__delete__` 中任意一个方法的对象。它是 `property`、`classmethod`、`staticmethod` 乃至 ORM 字段系统的共同底层机制。

描述符分为两类，其优先级差异是无数隐蔽 Bug 的根源：

| 类型 | 实现的方法 | 访问优先级 | 典型场景 |
| --- | --- | --- | --- |
| 数据描述符 | 实现了 `__set__` 或 `__delete__` | 高于实例 `__dict__` | 类型校验、ORM 字段、property |
| 非数据描述符 | 仅实现 `__get__` | 低于实例 `__dict__` | 方法、`classmethod`、缓存 |

这个优先级表直接决定了 `obj.attr` 的查找顺序：**先沿 MRO 找数据描述符，再查实例字典，最后才是非数据描述符与普通类属性。** 理解这一点，就能解释两个"反直觉"现象：

一是 `property` 是数据描述符，即使实例字典里写入了同名键，访问时依然走 `__get__`；二是普通函数是仅实现 `__get__` 的非数据描述符，所以实例可以"遮蔽"方法——当你给某个实例动态赋 `obj.method = something` 时，它会覆盖类上的方法绑定。

下面实现一个带缓存的非数据描述符，`cached_property`（Python 3.8 起已进入 `functools`）就是同类思路：

```python
class LazyProperty:
    """仅 __get__ 的非数据描述符，首次访问后缓存到实例字典。"""
    def __init__(self, func):
        functools.wraps(func)(self)
        self.func = func

    def __get__(self, obj, owner):
        if obj is None:
            return self
        value = self.func(obj)
        # 写入实例 __dict__，下次因非数据描述符优先级低而直接命中缓存
        obj.__dict__[self.func.__name__] = value
        return value
```

这里有一个生产级细节：`__get__` 必须处理 `obj is None`（即通过类访问 `Klass.attr` 的场景），否则框架在类级别反射属性时会抛 `TypeError`。同时，缓存写入实例字典而非描述符自身，是为了避免跨实例污染——每个实例各持一份缓存。

数据描述符的典型坑则体现在校验时机上。下面是一个带类型校验的字段描述符：

```python
class Field:
    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        return obj.__dict__.get(self.name)

    def __set__(self, obj, value):
        if not isinstance(value, int):
            raise TypeError(f"{self.name} must be int")
        obj.__dict__[self.name] = value
```

注意 `__set_name__` 钩子：它在类创建时被自动调用，让描述符无需显式传入属性名。若你的描述符要在多个属性上复用，务必用 `__set_name__` 而非 `__init__` 记录名字，否则不同实例会互相覆盖状态。

## 3. 元类：控制类的创建过程

元类是"类的类"。`type` 是所有类的默认元类，而自定义元类通过继承 `type` 并重写 `__new__` / `__init__` / `__call__`，分别拦截**类的创建**与**类的实例化**：

```python
class Meta(type):
    def __new__(mcls, name, bases, namespace, **kwargs):
        # 在类体执行完毕后、类对象生成前介入
        cls = super().__new__(mcls, name, bases, namespace)
        return cls

    def __call__(cls, *args, **kwargs):
        # 拦截 obj = Klass() 的实例化过程
        instance = super().__call__(*args, **kwargs)
        return instance

class Base(metaclass=Meta):
    pass
```

`__new__` 的 `namespace` 参数就是类体执行后收集到的命名空间字典，里面已经包含了方法、描述符对象与类属性。这正是 ORM、序列化框架与 `dataclasses` 在类创建时收集字段信息的注入点。

一个贯穿全文的综合示例——用元类实现类似 `dataclass` 的 `__repr__` 自动生成：

```python
class ReprMeta(type):
    def __new__(mcls, name, bases, namespace):
        if name != "Base":  # 跳过基类本身
            fields = [
                k for k, v in namespace.items()
                if isinstance(v, Field)
            ]
            if fields:
                def __repr__(self):
                    parts = ", ".join(
                        f"{f}={getattr(self, f)!r}" for f in fields
                    )
                    return f"{name}({parts})"
                namespace["__repr__"] = __repr__
        return super().__new__(mcls, name, bases, namespace)

class Base(metaclass=ReprMeta):
    pass

class Point(Base):
    x = Field()
    y = Field()

p = Point()
p.x = 1
p.y = 2
assert repr(p) == "Point(x=1, y=2)"
```

这里"元类 + 描述符"的组合展示了完整的协作链条：元类在 `__new__` 阶段遍历 `namespace`，识别出 `Field` 描述符实例，据此注入 `__repr__`；而 `Field` 的 `__set_name__` 在类创建时记录了属性名，二者共享同一次"类创建"生命周期。

## 4. 框架源码中的实战对照

脱离源码谈元编程都是空谈。以下三个真实案例展示了这套机制的工业级用法：

**Django ORM 的字段与 `ModelBase`。** Django 的模型字段（`IntegerField`、`CharField` 等）本质是数据描述符，`ModelBase` 是 `type` 的子类。它在 `__new__` 中遍历 `attrs`，把字段收集进 `_meta`，并生成反向关系、默认管理器等。生产中最常见的坑是**元类冲突**：当你给一个已是 `models.Model` 子类的模型再套一个自定义元类时，会抛 `TypeError: metaclass conflict`。解决方式是让自定义元类继承 `ModelBase`：

```python
from django.db.models.base import ModelBase

class MyMeta(ModelBase):
    def __new__(mcls, name, bases, namespace, **kwargs):
        # 自定义逻辑
        return super().__new__(mcls, name, bases, namespace, **kwargs)
```

**`abc.ABCMeta` 与 `__abstractmethods__`。** `ABCMeta.__new__` 通过检查类体中带 `@abstractmethod` 的方法，计算出 `__abstractmethods__` 集合；若集合非空，实例化时 `__call__` 直接抛 `TypeError`。这解释了为何抽象检查发生在实例化而非定义时，也解释了为何一个类只有继承了 `ABC`（或声明 `metaclass=ABCMeta`）后 `@abstractmethod` 才生效。

**`dataclasses` 的 `__post_init__`。** `dataclass` 装饰器本质是类装饰器，它读取 `Field` 描述符（`dataclasses.Field`）并在生成 `__init__` 时按字段顺序拼接赋值逻辑，最后在结尾调用 `__post_init__`。理解它的"描述符 + 代码生成"组合，就能明白为何继承含默认值字段的类时，默认值字段必须排在非默认值字段之后，否则生成器会抛 `TypeError: non-default argument follows default argument`。

## 5. 元类冲突与调试技巧

元类是最容易被滥用的特性。几条来自生产事故的纪律：

**排查思路一：`metaclass conflict` 的根因。** 当一个类的基类拥有不同的元类，且这些元类彼此无继承关系时，Python 无法合成唯一元类。用 `type(cls).__mro__` 检查基类元类链，让冲突元类继承最具体的那一个即可化解。

**排查思路二：属性访问异常时的定位顺序。** 遇到"明明赋值了却读不到"或"读取时报 `TypeError`"，按前文优先级表逐步验证：先 `hasattr(type(obj), '__set__')` 判断是否存在数据描述符，再查 `obj.__dict__`，最后检查 MRO 中的非数据描述符。一行 `obj.__dict__` 往往能瞬间区分"被描述符拦截"与"从未写入"。

**排查思路三：装饰器副作用。** 用 `inspect.unwrap` 剥离装饰器拿到原始函数，用 `func.__wrapped__` 链回溯。若怀疑是 `@wraps` 缺失导致的路由冲突，打印 `func.__name__` 与 `id(func)` 是第一步。

**调试建议：** 元类内部的 `print` 极难追踪，因为它在导入期运行。改用 `importlib.reload` 配合日志，或在 `__new__` 中加 `breakpoint()` 后 `python -m pdb` 单步，比散落的 print 高效得多。同时，给元类逻辑写单元测试时，记得用 `type.__new__` 直接构造类对象，绕过模块导入的干扰。

**滥用警告：** 元类适合"横向切面"式的统一约束（字段收集、接口校验、单例注册），不适合承载业务逻辑。一旦发现某个元类里塞进了几十行与"类创建"无关的代码，说明抽象层级放错了——业务规则应下沉到实例方法或独立函数。可读性永远是元编程的第一成本，第二个 Python 工程师看不懂的元类，就是下一个事故现场。

## 小结与建议

- **选型阶梯**：先函数装饰器，不够再类装饰器，最后才是元类；能不改属性解析规则就别上元类。
- **装饰器纪律**：一律使用 `@functools.wraps`；把重逻辑推迟到调用期，避免导入期副作用；参数化装饰器要分清"工厂/装饰器/对象"三层。
- **描述符纪律**：数据描述符优先级高于实例字典，非数据描述符反之；复用描述符务必用 `__set_name__` 记录属性名；`__get__` 要处理 `obj is None`。
- **元类纪律**：遇到 `metaclass conflict` 先查基类元类链；让自定义元类继承最具体的框架元类（如 Django 的 `ModelBase`）。
- **调试手段**：`inspect.unwrap`、`func.__wrapped__`、`type(cls).__mro__`、`obj.__dict__` 是元编程排障的四件套。
- **一句话心法**：装饰器包装行为，描述符拦截属性，元类重塑类型——它们共享同一条"一切皆对象"的主线，掌握了协议与优先级，就掌握了 Python 元编程的钥匙。
