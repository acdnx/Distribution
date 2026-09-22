# -*- coding: utf-8 -*-
"""MVSV 文件生成 —— 格式定义、数据模型与序列化（公共模块）。

本模块自 `QuoteCollectRunner.py` 提取，供任意需要产出 MVSV 的模块复用。

【职责】
    1. 格式定义：头部字段（缩写 / 长名 / 类型）与分钟级 / 日线级两套列序；
    2. 数据模型：`KlineMinBar`（分钟级 14 列）、`KLineDayBar`（日/周/月/年级 18 列），
       各自 `fromKlineBar` 完成一次性字段换算；
    3. 序列化：`buildMvsvName` 生成文件名、`writeMvsv` 写文件、
       `toMvsvFieldMap` 把模型转成「列名 → 文本」映射。

【主入口】
    writeMvsv(task, periodLabel, bars) -> Optional[Path]
        bars 为 `KlineMinBar` / `KLineDayBar` 列表；
        task 需含 region / market / usc / type_kline / label / symbol。

【依赖】
    **仅标准库**。刻意不依赖行情客户端库：`fromKlineBar` 的入参以结构化声明描述
    （只要求具备 timeKey / localDate / open / ... 等属性），任何数据源只要产出同形对象
    即可复用；同时避免与采集脚本形成循环依赖。

【输出位置】
    默认 `OUTPUT_DIR = <本文件目录>/../../../ZZFS/Finv/Quote`（原仓库布局）；
    调用方可改写模块级 `OUTPUT_DIR`（采集脚本即如此，改指系统临时目录）。
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: 数据模型入参刻意标注为 `Any`：真实类型是客户端库的 `KlineBar`，本模块不导入它
#: （避免循环依赖，也让本模块可脱离行情库独立复用）。运行时按鸭子类型使用，只要求
#: 对象具备 timeKey / localDate / time / open / high / low / close / volume / turnover /
#: changeRate / lastClose / timeZoneMinutes / scName 等属性。


#: MVSV 字段元数据：缩写 → （长名, 类型）。**列序不在这里**——两个级别的列序不同，
#: 由下面的 ``MVSV_MIN_FIELDS`` / ``MVSV_DAY_FIELDS`` 各自声明；标的名只在头部 `# Name`
#: 出现，故不占数据列。共 19 个候选字段
MVSV_FIELD_SPECS: Dict[str, Tuple[str, str]] = {
    "ts": ("Timestamp", "Int"),
    "d": ("Date", "Int"),
    "t": ("Time", "Int"),
    "o": ("Open", "Decimal"),
    "h": ("High", "Decimal"),
    "l": ("Low", "Decimal"),
    "c": ("Close", "Decimal"),
    "v": ("Volume", "Decimal"),
    "a": ("Turnover", "Decimal"),
    "cp": ("ChangePrice", "Decimal"),
    "cr": ("ChangeRatio", "Decimal"),
    "lc": ("LastClose", "Decimal"),
    "se": ("Session", "String"),
    "om": ("OffsetMinute", "Int"),
    "sp": ("SettlePrice", "Decimal"),
    "oi": ("OpenInterest", "Decimal"),
    "pe": ("PeRatio", "Decimal"),
    "tr": ("TurnoverRate", "Decimal"),
    "iv": ("ImpliedVolatility", "Decimal"),
}


#: MVSV 头部 Title 中的周期词（按调度表 type_kline 取值；未列出时回退为 TypeKLine 大驼峰值）
MVSV_TITLE_PERIODS: Dict[str, str] = {
    "MIN": "Minute", "MIN5": "5 Minute", "MIN10": "10 Minute",
    "HOUR": "Hourly", "HOUR2": "2 Hour", "HOUR3": "3 Hour", "HOUR6": "6 Hour",
    "DAY": "Daily", "WEEK": "Weekly", "MONTH": "Monthly", "YEAR": "Yearly",
}


DATA_PROVIDER = "FTMM"              # 写入 MVSV 头与文件名第 5 段的数据源标识


OUTPUT_DIR = Path(__file__).resolve().parents[3] / "ZZFS" / "Finv" / "Quote"


#: 调度表 region 取值 → 时区标识（写入 MVSV 头）
TIMEZONE_BY_REGION: Dict[str, str] = {
    "US": "America/New_York",
    "CN": "Asia/Shanghai",
    "HK": "Asia/Shanghai",
}


#: **分钟级**列序（14 列）：没有 `pe` / `tr` / `oi` / `iv`（分钟级无这些口径）与 `sp`
#: （接口不返回结算价）；`om` 紧随 `lc`，**`se` 为行末字段**
MVSV_MIN_FIELDS = ("ts", "d", "t", "o", "h", "l", "c", "v", "a", "cp", "cr", "lc", "om", "se")


#: **日 / 周 / 月 / 年级**列序（18 列）：`lc` 之后依次是结算价、持仓量与基本面三项，
#: **行末为 `om`**；没有 `se`（周期 K 线没有时段划分）
MVSV_DAY_FIELDS = ("ts", "d", "t", "o", "h", "l", "c", "v", "a", "cp", "cr", "lc",
                   "sp", "oi", "pe", "tr", "iv", "om")


#: 分钟级字段长名（与 ``MVSV_MIN_FIELDS`` 一一对应）
MVSV_MIN_FIELD_NAMES = tuple(MVSV_FIELD_SPECS[field][0] for field in MVSV_MIN_FIELDS)


#: 分钟级字段类型（与 ``MVSV_MIN_FIELDS`` 一一对应）
MVSV_MIN_FIELD_TYPES = tuple(MVSV_FIELD_SPECS[field][1] for field in MVSV_MIN_FIELDS)


#: 日 / 周 / 月 / 年级字段长名（与 ``MVSV_DAY_FIELDS`` 一一对应）
MVSV_DAY_FIELD_NAMES = tuple(MVSV_FIELD_SPECS[field][0] for field in MVSV_DAY_FIELDS)


#: 日 / 周 / 月 / 年级字段类型（与 ``MVSV_DAY_FIELDS`` 一一对应）
MVSV_DAY_FIELD_TYPES = tuple(MVSV_FIELD_SPECS[field][1] for field in MVSV_DAY_FIELDS)


#: 一根 K 线覆盖多日的周期类型（type_kline 取值）→ 按**整段区间**取数。
#: 这类 K 线逐日请求毫无意义（一天只回一根），故对 ``[start, end)`` 一次请求取全；
#: 分钟级类型仍走「逐日循环 + 按标的类型分流」的取数策略。
#: 两处使用：MVSV 序列化据此选择字段集（本模块），采集据此选择取数策略（采集脚本）。
INTERVAL_KLINE_TYPES = ("DAY", "WEEK", "MONTH", "YEAR")


@dataclass
class KlineMinBar:
    """分钟级单根 K 线（字段即 MVSV 数据列，共 14 列 + 头部用 ``name``）。

    Attributes:
        timestamp: 秒级时间戳（UTC），对应 MVSV 列 ``Timestamp``。
        date: 标的本地日期整数 ``YYYYMMDD``，对应 ``Date``。
        time: 标的本地时间整数 ``HHMMSS``，对应 ``Time``。
        open: 开盘价。
        high: 最高价。
        low: 最低价。
        close: 收盘价。
        volume: 成交量。
        turnover: 成交额。
        changePrice: 涨跌额（``close - lastClose`` 现算），对应 ``ChangePrice``。
        changeRatio: 涨跌幅（百分数），对应 ``ChangeRatio``。
        lastClose: 昨收价，对应 ``LastClose``。
        offsetMinute: 该行所属时区偏移（**分钟**），对应 ``OffsetMinute``。
        session: MVSV 时段标识（**行末字段**）——美股取 ``PRE`` 盘前 / ``RTH`` 盘中 /
            ``POST`` 盘后 / ``ONT`` 夜盘四档；非美股（无盘前盘后与夜盘划分）取空串。
            由 ``applySessions`` 按市场重写，故 ``fromKlineBar`` 里先透传客户端的原值。
        name: 标的显示名（中文名优先）——**不是数据列**，只供文件头部 ``# Name`` 使用。
    """

    timestamp: int = 0
    date: int = 0
    time: int = 0
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    turnover: Optional[float] = None
    changePrice: Optional[float] = None
    changeRatio: Optional[float] = None
    lastClose: Optional[float] = None
    offsetMinute: int = 0
    session: str = ""
    name: Optional[str] = None

    @classmethod
    def fromKlineBar(cls, bar: Any) -> "KlineMinBar":
        """由客户端 ``KlineBar`` 构造本模型（顺带完成一次性的字段换算）。

        Args:
            bar: 客户端产出的 ``KlineBar``。

        Returns:
            对应的 ``KlineMinBar``。
        """
        dateText = str(bar.localDate or "").replace("-", "")
        timeText = str(bar.time or "").split(" ")[-1].replace(":", "")
        changePrice = None
        if bar.close is not None and bar.lastClose is not None:
            changePrice = bar.close - bar.lastClose
        return cls(
            timestamp=bar.timeKey // 1000,
            date=int(dateText) if dateText.isdigit() else 0,
            time=int(timeText) if timeText.isdigit() else 0,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            turnover=bar.turnover,
            changePrice=changePrice,
            changeRatio=bar.changeRate,
            lastClose=bar.lastClose,
            offsetMinute=bar.timeZoneMinutes,
            session=bar.session,
            name=bar.scName or bar.name,
        )

    @property
    def hasPrice(self) -> bool:
        """是否含有效价格（四价至少一项非空）。

        未产生行情的时间点会返回缺 OHLC 的占位记录，可用本属性过滤。

        Returns:
            True 表示含有效价格数据。
        """
        return any(value is not None for value in (self.open, self.high, self.low, self.close))

    def toDict(self) -> Dict[str, Any]:
        """转为字典（剔除值为 None 的字段，便于直接落盘为 JSONL）。

        Returns:
            字段名与属性同名的字典。
        """
        return {key: value for key, value in vars(self).items() if value is not None}


def toMinBars(bars: Iterable[Any]) -> List[KlineMinBar]:
    """把客户端的 ``KlineBar`` 序列转换为分钟级模型。

    这是**数据处理环节**的入口：取数部分（示例的 ``fetchDayBars`` 等）保持原样、
    依旧产出 ``KlineBar``，字段换算与命名标准化只在这里发生，基础接口不受影响。
    转换后通常还要调用 ``applySessions`` 按市场重写时段。

    Args:
        bars: ``KlineBar`` 序列。

    Returns:
        ``KlineMinBar`` 列表（保持输入顺序）。
    """
    return [KlineMinBar.fromKlineBar(bar) for bar in bars]


@dataclass
class KLineDayBar:
    """日 / 周 / 月 / 年级单根 K 线（字段即 MVSV 数据列，共 18 列 + 头部用 ``name``）。

    Attributes:
        timestamp: 秒级时间戳（UTC），对应 MVSV 列 ``Timestamp``。
        date: 标的本地日期整数 ``YYYYMMDD``，对应 ``Date``。
        time: 标的本地时间整数 ``HHMMSS``（周期 K 线为 ``0``），对应 ``Time``。
        open: 开盘价。
        high: 最高价。
        low: 最低价。
        close: 收盘价。
        volume: 成交量。
        turnover: 成交额。
        changePrice: 涨跌额（``close - lastClose`` 现算），对应 ``ChangePrice``。
        changeRatio: 涨跌幅（百分数），对应 ``ChangeRatio``。
        lastClose: 昨收价，对应 ``LastClose``。
        settlePrice: 结算价（期货 / 期权，正股为 None），对应 ``SettlePrice``。
        openInterest: 持仓量（期货 / 期权），对应 ``OpenInterest``。
        peRatio: 市盈率，对应 ``PeRatio``。
        turnoverRate: 换手率（百分数），对应 ``TurnoverRate``。
        impliedVolatility: 隐含波动率（期权），对应 ``ImpliedVolatility``。
        offsetMinute: 该行所属时区偏移（**分钟**），对应 ``OffsetMinute``。
        name: 标的显示名（中文名优先）——**不是数据列**，只供文件头部 ``# Name`` 使用。
    """

    timestamp: int = 0
    date: int = 0
    time: int = 0
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    turnover: Optional[float] = None
    changePrice: Optional[float] = None
    changeRatio: Optional[float] = None
    lastClose: Optional[float] = None
    settlePrice: Optional[float] = None
    openInterest: Optional[float] = None
    peRatio: Optional[float] = None
    turnoverRate: Optional[float] = None
    impliedVolatility: Optional[float] = None
    offsetMinute: int = 0
    name: Optional[str] = None

    @classmethod
    def fromKlineBar(cls, bar: Any) -> "KLineDayBar":
        """由客户端 ``KlineBar`` 构造本模型（顺带完成一次性的字段换算）。

        Args:
            bar: 客户端产出的 ``KlineBar``。

        Returns:
            对应的 ``KLineDayBar``。
        """
        dateText = str(bar.localDate or "").replace("-", "")
        timeText = str(bar.time or "").split(" ")[-1].replace(":", "")
        changePrice = None
        if bar.close is not None and bar.lastClose is not None:
            changePrice = bar.close - bar.lastClose
        return cls(
            timestamp=bar.timeKey // 1000,
            date=int(dateText) if dateText.isdigit() else 0,
            time=int(timeText) if timeText.isdigit() else 0,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            turnover=bar.turnover,
            changePrice=changePrice,
            changeRatio=bar.changeRate,
            lastClose=bar.lastClose,
            settlePrice=bar.settlePrice,
            openInterest=bar.openInterest,
            peRatio=bar.peRatio,
            turnoverRate=bar.turnoverRate,
            impliedVolatility=bar.impliedVolatility,
            offsetMinute=bar.timeZoneMinutes,
            name=bar.scName or bar.name,
        )

    @property
    def hasPrice(self) -> bool:
        """是否含有效价格（四价至少一项非空）。

        Returns:
            True 表示含有效价格数据。
        """
        return any(value is not None for value in (self.open, self.high, self.low, self.close))

    def toDict(self) -> Dict[str, Any]:
        """转为字典（剔除值为 None 的字段，便于直接落盘为 JSONL）。

        Returns:
            字段名与属性同名的字典。
        """
        return {key: value for key, value in vars(self).items() if value is not None}


def toDayBars(bars: Iterable[Any]) -> List[KLineDayBar]:
    """把客户端的 ``KlineBar`` 序列转换为日 / 周 / 月 / 年级模型。

    与 ``KlineMinBar.toMinBars`` 的唯一差别是多带 5 个周期 K 线专有字段，换算规则完全一致。

    Args:
        bars: ``KlineBar`` 序列（日 / 周 / 月 / 年级任务整段区间取数的产出）。

    Returns:
        ``KLineDayBar`` 列表（保持输入顺序）。
    """
    return [KLineDayBar.fromKlineBar(bar) for bar in bars]


def toMvsvFieldMap(bar: Any) -> Dict[str, str]:
    """把单根 K 线**模型**转为 MVSV 数据行的 ``{字段缩写: 文本}`` 映射（键同 ``MVSV_FIELD_SPECS``）。

    模型（``KlineMinBar`` / ``KLineDayBar``）的字段名就是 MVSV 列名的小驼峰，因此这里只做
    **文本化**，不再有任何名字映射与数值换算——换算（时间戳取秒、日期时间整数化、涨跌额现算）
    已在模型的 ``fromKlineBar`` 里一次性完成。返回映射而非定长列表，是为了让字段集与序列化
    解耦：分钟级取 ``MVSV_MIN_FIELDS``（14 列）、日 / 周 / 月 / 年级取 ``MVSV_DAY_FIELDS``
    （18 列），二者共用本函数。

    文本化规则：数值缺失留空（与参考 MVSV 文件的写法一致）；``ChangePrice`` 与 ``ChangeRatio``
    按定点 6 位小数输出（不足位右补 0）；``Time`` 补足 6 位（``000000``）。时段列 ``se`` 直接取
    模型已写入的值（美股 ``PRE`` / ``RTH`` / ``POST`` / ``ONT``，其他市场为空）。

    Args:
        bar: ``KlineMinBar``（分钟级）或 ``KLineDayBar``（日 / 周 / 月 / 年级）实例。

    Returns:
        ``{字段缩写: 文本}`` 映射，键与 ``MVSV_FIELD_SPECS`` 一致（19 项）：分钟级模型不含
        ``pe`` / ``tr`` / ``oi`` / ``iv`` / ``sp``，日线级模型不含 ``se``，取不到值的列留空，
        且不会被 ``writeMvsv()`` 导出。
    """
    return {
        "ts": str(bar.timestamp),
        "d": str(bar.date),
        "t": f"{bar.time:06d}",
        "o": _text(bar.open),
        "h": _text(bar.high),
        "l": _text(bar.low),
        "c": _text(bar.close),
        "v": _text(bar.volume),
        "a": _text(bar.turnover),
        "cp": _fixed6(bar.changePrice),
        "cr": _fixed6(bar.changeRatio),
        "lc": _text(bar.lastClose),
        "se": _sessionOf(bar),
        "om": str(bar.offsetMinute),
        "pe": _text(getattr(bar, "peRatio", None)),
        "tr": _text(getattr(bar, "turnoverRate", None)),
        "oi": _text(getattr(bar, "openInterest", None)),
        "iv": _text(getattr(bar, "impliedVolatility", None)),
        "sp": _text(getattr(bar, "settlePrice", None)),
    }


def _sessionOf(bar: Any) -> str:
    """取模型里的时段原值（日线级模型没有 ``session`` 字段，返回空串）。

    Args:
        bar: K 线模型实例。

    Returns:
        时段文本（``RTH`` / ``ETH`` / ``OVERNIGHT``）或空串。
    """
    return str(getattr(bar, "session", "") or "")


def buildMvsvName(task: Dict[str, Any], periodLabel: str) -> str:
    """按归档任务拼出 MVSV 文件名。

    形如 ``US_COMEX_GCMain_Day_FTMM_Mon_20260921.mvsv``——第 4 段是 K 线周期、第 6 段是调度表的
    ``period``，两段都按**大驼峰**输出（``MIN`` / ``Min`` → ``Min``，``DAY`` / ``Day`` → ``Day``，
    ``MON`` / ``Mon`` → ``Mon``）。周期标识紧随数据源之后：日标签为 ``YYYYMMDD``、月标签为
    ``YYYYMM``、周标签为 ``YYYYWW``，仅凭标签数字无法分辨周期，故显式写入。

    Args:
        task: 归档任务字典（提供 region / market / usc / type_kline / label）。
        periodLabel: 周期标识（大驼峰，如 ``Day`` / ``Mon`` / ``Week``）。

    Returns:
        文件名（不含目录）。
    """
    return (f"{task['region']}_{task['market']}_{task['usc']}_{_typeKlineLabel(task)}_"
            f"{DATA_PROVIDER}_{periodLabel}_{task['label']}.mvsv")


def writeMvsv(task: Dict[str, Any], periodLabel: str, bars: List[Any]) -> Optional[Path]:
    """把任务区间的 K 线写入 MVSV 文件（覆盖写）。

    头部按 14 行元信息写：``Title`` / ``DataProvider`` / ``Field`` / ``FieldName`` /
    ``FieldType`` / ``TypeKLine`` / ``Count`` / ``FetchTime`` / ``Symbol`` / ``Region`` /
    ``Market`` / ``USC`` / ``Name`` / ``TimeZone``，其中 ``Title`` 的周期词与 ``TypeKLine``
    均来自任务的 ``type_kline``（``MIN`` → ``Minute`` / ``Min``，``DAY`` → ``Daily`` / ``Day``），
    文件名第 4 段则沿用全大写原值。

    **字段集按级别切换**：分钟级任务只导出 ``MVSV_MIN_FIELDS``（14 列，不含 ``pe`` / ``tr`` /
    ``oi`` / ``iv`` / ``sp``），日 / 周 / 月 / 年级任务导出 ``MVSV_DAY_FIELDS``（18 列，
    保留上述 5 列但**不含** ``se``）。

    Args:
        task: 归档任务字典（提供 region / market / usc / type_kline / label / symbol）。
        periodLabel: 周期标识（大驼峰，如 ``Day`` / ``Mon`` / ``Week``），参与文件名。
        bars: 已过滤并去重的 K 线**模型**列表（``KlineMinBar`` 或 ``KLineDayBar``）。

    Returns:
        写入的文件路径；无数据时不创建文件并返回 None。
    """
    if not bars:
        print("[跳过] 任务区间内无数据，不创建文件")
        return None
    typeKline = _typeKline(task)
    periodWord = MVSV_TITLE_PERIODS.get(typeKline, _typeKlineLabel(task))
    if typeKline in INTERVAL_KLINE_TYPES:
        fields, fieldNames, fieldTypes = (MVSV_DAY_FIELDS, MVSV_DAY_FIELD_NAMES,
                                          MVSV_DAY_FIELD_TYPES)
    else:
        fields, fieldNames, fieldTypes = (MVSV_MIN_FIELDS, MVSV_MIN_FIELD_NAMES,
                                          MVSV_MIN_FIELD_TYPES)
    target = OUTPUT_DIR / buildMvsvName(task, periodLabel)
    tmpPath = target.with_suffix(target.suffix + ".tmp")
    with tmpPath.open("w", encoding="utf-8", newline="\n") as handle:
        header = [
            f"# Title : {task['usc']} {periodWord} Quote Data",
            f"# DataProvider : {DATA_PROVIDER}",
            f'# Field : "{"|".join(fields)}"',
            f'# FieldName : "{"|".join(fieldNames)}"',
            f'# FieldType : "{"|".join(fieldTypes)}"',
            f"# TypeKLine : {_typeKlineLabel(task)}",
            f"# Count : {len(bars)}",
            f'# FetchTime : "{datetime.now().astimezone().isoformat(timespec="seconds")}"',
            f"# Symbol : {task['symbol']}",
            f"# Region : {task['region']}",
            f"# Market : {task['market']}",
            f"# USC : {task['usc']}",
            f'# Name : "{bars[0].name or task["usc"]}"',
            f"# TimeZone : {TIMEZONE_BY_REGION.get(task['region'], 'UTC')}",
        ]
        # 头部与数据集之间保留一行空行
        handle.write("\n".join(header) + "\n\n")
        for bar in bars:
            fieldMap = toMvsvFieldMap(bar)
            handle.write("|".join(fieldMap[field] for field in fields))
            handle.write("\n")
    tmpPath.replace(target)
    size = target.stat().st_size
    print(f"[落盘] {target.name}｜{len(bars)} 行｜{size:,} 字节｜{len(fields)} 列｜"
          f"{bars[0].date} {bars[0].time:06d} ~ {bars[-1].date} {bars[-1].time:06d}")
    return target


def _toPascal(value: Any) -> str:
    """把调度表取值转为大驼峰文本。

    ``type_kline`` 与 ``period`` 两列的取值统一按大驼峰输出：``DAY`` / ``Day`` → ``Day``，
    ``MON`` / ``Mon`` → ``Mon``，``MIN5`` / ``Min5`` → ``Min5``（历史数据里的全大写与新版
    调度表的大驼峰都能正确处理）。

    Args:
        value: 调度表字段值（大小写不限）。

    Returns:
        大驼峰文本；空值时返回空串。
    """
    return str(value or "").capitalize()


def _typeKlineLabel(task: Dict[str, Any]) -> str:
    """取任务的 K 线周期类型**大驼峰**文本，用于文件名第 4 段与 MVSV 头部 ``TypeKLine``。

    取 ``_typeKline`` 结果的大驼峰形式：``MIN`` → ``Min``、``MIN5`` → ``Min5``、
    ``MIN10`` → ``Min10``、``HOUR2`` → ``Hour2``、``MONTH`` → ``Month``。

    Args:
        task: 归档任务字典（提供 type_kline）。

    Returns:
        大驼峰周期类型文本（如 ``Min``）；字段缺失时返回空串。
    """
    return _toPascal(task.get("type_kline"))


def _typeKline(task: Dict[str, Any]) -> str:
    """取任务的 K 线周期类型（归一化为大写），用于 MVSV 文件名的第 4 段。

    标准取值为 ``MIN`` / ``MIN5`` / ``MIN10`` / ``HOUR`` / ``HOUR2`` / ``HOUR3`` /
    ``HOUR6`` / ``DAY`` / ``WEEK`` / ``MONTH`` / ``YEAR``，未列出的取值原样输出。

    Args:
        task: 归档任务字典（提供 type_kline）。

    Returns:
        大写的周期类型文本（如 ``MIN``）；字段缺失时返回空串。
    """
    return str(task.get("type_kline", "")).upper()


def _text(value: Optional[float]) -> str:
    """把可空数值转为字段文本（``None`` → 空串）。

    Args:
        value: 原始值。

    Returns:
        数值文本或空串。
    """
    return "" if value is None else str(value)


def _fixed6(value: Optional[float]) -> str:
    """把可空数值格式化为定点 6 位小数文本（不足位右补 0）；``None`` → 空串。

    涨跌额（``ChangePrice``）与涨跌幅（``ChangeRatio``）用本函数输出：浮点差值会出现长尾
    （如 ``0.10826371999999651``），接口原值也可达 17 位有效数字（如 ``0.10074182600727029``），
    按定点 6 位输出既稳定又可读（``0.108264`` / ``0.100742``）。

    Args:
        value: 原始数值。

    Returns:
        形如 ``0.100742`` 的文本或空串。
    """
    return "" if value is None else f"{value:.6f}"

