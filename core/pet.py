"""桌面宠物：状态机、饥饿/困意/精力模拟与猫粮经济。

设计要点
========
* **无第三方依赖**：纯标准库 + 调用方注入时钟，方便离屏测试。
* **状态机**：idle / playing(玩球) / parkour(跑酷) / sleeping / hungry / eating /
  drinking 七态。转移由三属性驱动：
    - hunger  0(饱)~100(饿)，随在线时间增长；≥70 触发饿了气泡。
    - energy  0(疲惫)~100(充沛)；>60 才会玩球/跑酷，<25 困意加深。
    - sleepiness 0(清醒)~100(困)；≥80 强制趴下睡觉。
* **猫粮经济**（用户规则）：
    - 播放器每在线 1 小时获得 1 份猫粮 + 2 份水（水粮比 2:1，对应宠物
      一天两餐水更频繁的自然习性）；
    - 每日（按本地日历日）最多累积 3 小时 = 3 份粮 + 6 份水，超出不再记账；
    - 每投喂一次粮 -1、水 -1；
    - 食量定义：1 份粮 = 一只宠物一天的食量，饱食度由 hunger 承载。
* **持久化**：全部状态走 settings 表（复用现有 SQLite），键统一
  ``pet.`` 前缀，与其它配置互不干扰。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

# ---------------------------------------------------------------- constants

FEED_PER_HOUR = 1            # 每在线 1 小时猫粮 +1
WATER_PER_HOUR = 2           # 每 1 小时同时获得的水（水粮比 2:1）
DAILY_HOUR_CAP = 3           # 每日最多记账 3 小时（超出不再获取）
HUNGER_PER_HOUR = 40.0       # 每在线小时饥饿度增长（约 2.5h 从饱到饿）
ENERGY_DRAIN_PER_HOUR = 20.0 # 清醒时精力流失
ENERGY_PLAY_COST = 18.0      # 玩一次球/跑酷的精力开销
SLEEP_ENERGY_GAIN_PER_MIN = 8.0
SLEEP_HUNGER_GAIN_PER_MIN = 0.8   # 睡觉也 mildly 消耗饥饿
SLEEPINESS_PER_HOUR = 26.0
SLEEP_RECOVER_PER_MIN = 45.0      # 睡觉困意消退速度
HUNGER_ALERT = 70.0               # ≥ 此值进入 hungry 气泡状态
SLEEPINESS_FORCE_SLEEP = 80.0
ENERGY_PLAY_MIN = 60.0            # 精力 ≥ 此值才会主动玩
IDLE_PLAY_CHANCE = 0.30           # 每次 idle 轮询时尝试玩耍的概率


class PetState(str, Enum):
    IDLE = "idle"           # 站立/左右小踱步
    PLAYING = "playing"     # 玩球
    PARKOUR = "parkour"     # 跑酷（沿屏幕边缘跑圈）
    SLEEPING = "sleeping"   # 趴下睡觉
    HUNGRY = "hungry"       # 站着 + 饿了气泡
    EATING = "eating"       # 低头饭盆
    DRINKING = "drinking"   # 低头水盆
    HAPPY = "happy"         # 被摸头/点击后的开心蹦跳（互动）
    ANGRY = "angry"         # 睡觉被硬叫醒，弓背炸毛（互动）
    STRETCHING = "stretching"  # 伸懒腰（睡醒后优先，idle 偶尔来一个）


# 互动瞬时状态：播放期内保持，到期自动回 idle；被 decide 的常规分支接管。
INTERACTION_STATES = (PetState.HAPPY, PetState.ANGRY, PetState.STRETCHING)


# 品种库：外观皮肤 = 毛色 + 花纹 + 耳型；动作骨架全品种共用。
# pattern: solid 纯色 / tabby 条纹 / calico 三花斑块 / point 重点色(耳尾深) /
#          shaded 渐层(顶深腹浅)
# ear:     pointed 尖耳 / big 大耳(德文) / floppy 垂耳(狗)
BREEDS: dict[str, dict[str, Any]] = {
    "orange":  {"label": "橘猫", "body": "#F5B971", "belly": "#FBDFB2",
                "pattern_color": "#DE9440", "pattern": "tabby", "ear": "pointed"},
    "lihua":   {"label": "狸花猫", "body": "#A08262", "belly": "#CDB495",
                "pattern_color": "#4E3B28", "pattern": "tabby", "ear": "pointed"},
    "calico":  {"label": "三花猫", "body": "#F7F2E8", "belly": "#FFFFFF",
                "pattern_color": "#E0913C", "pattern": "calico", "ear": "pointed"},
    "ragdoll": {"label": "布偶猫", "body": "#F2EDE6", "belly": "#FFFFFF",
                "pattern_color": "#B09A85", "pattern": "point", "ear": "pointed",
                "blue_eyes": True},
    "british": {"label": "英短蓝猫", "body": "#93A0B4", "belly": "#ACB7C6",
                "pattern_color": "#7B889C", "pattern": "solid", "ear": "pointed"},
    "silver":  {"label": "银渐层", "body": "#E3E8EC", "belly": "#FAFBFC",
                "pattern_color": "#93A0AD", "pattern": "shaded", "ear": "pointed"},
    "golden":  {"label": "金渐层", "body": "#EFD9A0", "belly": "#F9EED2",
                "pattern_color": "#C6A266", "pattern": "shaded", "ear": "pointed"},
    "devon":   {"label": "德文卷毛猫", "body": "#C9C3BC", "belly": "#E0DBD4",
                "pattern_color": "#AFA8A0", "pattern": "solid", "ear": "big"},
    "dog":     {"label": "小狗", "body": "#E8C58F", "belly": "#F7E3C2",
                "pattern_color": "#C89A5F", "pattern": "solid", "ear": "floppy"},
}

DEFAULT_BREED = "orange"


def breed_label(breed_id: str) -> str:
    return BREEDS.get(breed_id, {}).get("label", breed_id)


# 宠物台词（气泡文案）
SAYINGS = {
    "hungry": ("肚子咕咕叫了…", "饭盆空空的呀", "想吃东西了，喵呜…", "汪！狗罐头！"),
    "thirsty": ("水碗见底啦", "好渴…有水吗"),
    "sleepy": ("眼皮好重…先睡了", "zzZ…"),
    "happy": ("陪我玩球嘛！", "看我跑酷！", "嘿嘿～"),
    "thanks": ("谢谢投喂！", "真好吃！"),
    "idle": ("今天听什么歌？", "陪你听歌～"),
    "petted": ("呼噜呼噜…", "好舒服，再摸摸～", "嘿嘿，最喜欢你了"),
    "woken_up": ("哼！气死了！", "人家睡得好好的！", "嗷呜——！"),
    "stretch": ("伸个懒腰～", "睡饱啦！"),
    "angry": ("哼！", "生气了！不理你了"),
}


@dataclass
class PetWallet:
    """猫粮与水的库存 + 每日记账。"""

    food: int = 0
    water: int = 0
    earned_seconds_today: float = 0.0
    earn_day: str = ""          # YYYY-MM-DD，跨日清零
    fed_meals_today: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "food": self.food,
            "water": self.water,
            "earned_seconds_today": round(self.earned_seconds_today, 1),
            "earn_day": self.earn_day,
            "fed_meals_today": self.fed_meals_today,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PetWallet":
        data = data or {}

        def _i(key: str, default: int = 0) -> int:
            try:
                return int(data.get(key))
            except (TypeError, ValueError):
                return default

        def _f(key: str, default: float = 0.0) -> float:
            try:
                return float(data.get(key))
            except (TypeError, ValueError):
                return default

        return cls(
            food=_i("food"),
            water=_i("water"),
            earned_seconds_today=_f("earned_seconds_today"),
            earn_day=str(data.get("earn_day") or ""),
            fed_meals_today=_i("fed_meals_today"),
        )


@dataclass
class PetMood:
    hunger: float = 20.0        # 开局微饿
    energy: float = 90.0
    sleepiness: float = 10.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hunger": round(self.hunger, 1),
            "energy": round(self.energy, 1),
            "sleepiness": round(self.sleepiness, 1),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PetMood":
        data = data or {}
        # 注意不能写 ``x or 默认值``：hunger=0.0 是合法值（刚吃饱），
        # falsy 会被误判为缺失而回退默认。
        def _f(key: str, default: float) -> float:
            value = data.get(key)
            try:
                return float(value)
            except (TypeError, ValueError):
                return default
        return cls(
            hunger=_f("hunger", 20.0),
            energy=_f("energy", 90.0),
            sleepiness=_f("sleepiness", 10.0),
        )


class PetBrain:
    """宠物核心：属性演化 + 状态决策 + 猫粮记账。UI 层订阅 state_changed。"""

    def __init__(
        self,
        wallet: PetWallet | None = None,
        mood: PetMood | None = None,
        now_fn: Callable[[], float] | None = None,
        day_fn: Callable[[], str] | None = None,
    ) -> None:
        self.wallet = wallet or PetWallet()
        self.mood = mood or PetMood()
        self.state: PetState = PetState.IDLE
        self._now = now_fn or time.time
        self._day = day_fn or (lambda: time.strftime("%Y-%m-%d"))
        self._mood_ts: float = self._now()
        self._earn_ts: float = self._now()
        self._state_ts: float = self._now()
        self._speech: str = ""
        self._action_until: float = 0.0     # 主动作（玩球/跑酷）截止时刻

    # ------------------------------------------------------------ persistence

    def export(self) -> dict[str, Any]:
        return {"wallet": self.wallet.to_dict(), "mood": self.mood.to_dict()}

    def apply_saved(self, data: dict[str, Any]) -> None:
        """载入持久化状态并补算离线时段（离线不涨饥饿——爱宠不打烊）。"""
        if not isinstance(data, dict):
            return
        self.wallet = PetWallet.from_dict(data.get("wallet"))
        self.mood = PetMood.from_dict(data.get("mood"))
        self._mood_ts = self._earn_ts = self._state_ts = self._now()
        self._action_until = 0.0

    # ------------------------------------------------------------ earning

    def tick_earnings(self) -> int:
        """按在线时长记账猫粮/水；返回本次新增的粮数（UI 可弹提示）。"""
        today = self._day()
        if self.wallet.earn_day != today:
            self.wallet.earn_day = today
            self.wallet.earned_seconds_today = 0.0
            self.wallet.fed_meals_today = 0

        now = self._now()
        delta = min(now - self._earn_ts, 3600)   # 异常时钟跳变兜底
        self._earn_ts = now
        if delta <= 0:
            return 0
        room_hours = max(0.0, DAILY_HOUR_CAP - self.wallet.earned_seconds_today / 3600.0)
        credited = min(delta / 3600.0, room_hours)
        if credited <= 0:
            return 0
        # 每 80ms 就会 tick 一次：只有跨过整数小时边界才发粮/水，
        # 否则一次 tick 发一份，粮会在一小时内爆炸成几百份。
        hours_before = int(self.wallet.earned_seconds_today / 3600.0)
        self.wallet.earned_seconds_today += credited * 3600.0
        hours_after = int(self.wallet.earned_seconds_today / 3600.0)
        gained = hours_after - hours_before
        if gained > 0:
            self.wallet.food += FEED_PER_HOUR * gained
            self.wallet.water += WATER_PER_HOUR * gained
        return gained

    # ------------------------------------------------------------ needs tick

    def tick_mood(self, sleeping_override: bool = False) -> None:
        """推进饥饿/精力/困意；dt 为真实在线时长。"""
        now = self._now()
        dt_min = (now - self._mood_ts) / 60.0
        self._mood_ts = now
        if dt_min <= 0:
            return
        if sleeping_override or self.state is PetState.SLEEPING:
            self.mood.energy = min(100.0, self.mood.energy + SLEEP_ENERGY_GAIN_PER_MIN * dt_min)
            self.mood.sleepiness = max(0.0, self.mood.sleepiness - SLEEP_RECOVER_PER_MIN * dt_min)
            self.mood.hunger = min(100.0, self.mood.hunger + SLEEP_HUNGER_GAIN_PER_MIN * dt_min)
            return
        hours = dt_min / 60.0
        self.mood.hunger = min(100.0, self.mood.hunger + HUNGER_PER_HOUR * hours)
        self.mood.sleepiness = min(100.0, self.mood.sleepiness + SLEEPINESS_PER_HOUR * hours)
        if self.state in (PetState.PLAYING, PetState.PARKOUR):
            self.mood.energy = max(0.0, self.mood.energy - ENERGY_PLAY_COST * (dt_min * 10))
        else:
            self.mood.energy = max(0.0, self.mood.energy - ENERGY_DRAIN_PER_HOUR * hours)

    # ------------------------------------------------------------ interactions

    def pet_head(self) -> tuple[bool, str]:
        """用户点击/摸头：开心蹦跳；但睡梦中摸会被拍开（小概率生气）。"""
        if self.state is PetState.SLEEPING:
            return False, "嘘——人家在睡觉，别吵"
        self._enter(PetState.HAPPY, self._now())
        self.say("petted")
        self.mood.energy = min(100.0, self.mood.energy + 5.0)
        return True, "呼噜呼噜…"

    def wake_up(self) -> tuple[bool, str]:
        """把睡着的宠物硬叫醒：弓背炸毛生气一阵子。"""
        if self.state is not PetState.SLEEPING:
            return False, "人家醒着呢"
        self._enter(PetState.ANGRY, self._now())
        self._action_until = self._now() + 6.0
        self.say("woken_up")
        self.mood.sleepiness = max(0.0, self.mood.sleepiness - 30.0)
        return True, "哼！气死了！"

    # ------------------------------------------------------------ decisions

    def decide(self) -> PetState:
        """根据当前属性决定状态；主动作有最短持续时长避免抖动。"""
        now = self._now()
        self.tick_mood()

        # 互动瞬时状态：到期自动回落（摸头 3s / 生气 6s / 伸懒腰 2.5s）
        if self.state in INTERACTION_STATES:
            if now < self._action_until:
                return self.state
            return self._enter(PetState.IDLE, now)

        if self.state is PetState.SLEEPING:
            if self.mood.sleepiness < 25.0:
                # 自然睡醒：先伸个懒腰再恢复日常
                self._action_until = now + 2.5
                self.say("stretch")
                return self._enter(PetState.STRETCHING, now)
            return PetState.SLEEPING

        if self.mood.sleepiness >= SLEEPINESS_FORCE_SLEEP:
            return self._enter_sleep(now)

        if self.mood.hunger >= HUNGER_ALERT:
            return self._enter(PetState.HUNGRY, now)

        # 主动作播放期内保持
        if self.state in (PetState.PLAYING, PetState.PARKOUR) and now < self._action_until:
            return self.state

        # 精力旺盛时随机起兴
        if self.mood.energy >= ENERGY_PLAY_MIN and self.state is PetState.IDLE:
            if (now % 60) / 60.0 < IDLE_PLAY_CHANCE:
                return self._enter(
                    PetState.PARKOUR if self.mood.energy > 80 else PetState.PLAYING, now
                )
            # 偶尔伸个懒腰（每次 idle 轮询 3% 概率）
            if (now % 100) / 100.0 < 0.03:
                self._action_until = now + 2.5
                self.say("stretch")
                return self._enter(PetState.STRETCHING, now)

        if self.state in (PetState.PLAYING, PetState.PARKOUR):
            return self._enter(PetState.IDLE, now)

        if self.state is PetState.HUNGRY and self.mood.hunger < HUNGER_ALERT - 20:
            return self._enter(PetState.IDLE, now)
        return self.state

    def _enter(self, state: PetState, now: float) -> PetState:
        self.state = state
        self._state_ts = now
        if state is PetState.PLAYING:
            self._action_until = now + 12.0
            self.say("happy")
        elif state is PetState.PARKOUR:
            self._action_until = now + 18.0
            self.say("happy")
        elif state is PetState.HUNGRY:
            self.say("hungry")
        elif state is PetState.HAPPY:
            self._action_until = now + 3.0
        elif state is PetState.IDLE:
            self._speech = ""
        return state

    def _enter_sleep(self, now: float) -> PetState:
        self.say("sleepy")
        return self._enter(PetState.SLEEPING, now)

    # ------------------------------------------------------------ user feed

    def feed(self) -> tuple[bool, str]:
        """投喂猫粮；返回 (成功?, 提示语)。"""
        if self.wallet.food <= 0:
            return False, "猫粮不够啦，多听歌才能赚猫粮哦"
        self.wallet.food -= 1
        self.wallet.fed_meals_today += 1
        self.mood.hunger = max(0.0, self.mood.hunger - 55.0)
        self.mood.energy = min(100.0, self.mood.energy + 15.0)
        if self.state is PetState.SLEEPING:
            self.state = PetState.IDLE
            self._state_ts = self._now()
        self.say("thanks")
        return True, "投喂成功，吃得很香！"

    def give_water(self) -> tuple[bool, str]:
        """加水；返回 (成功?, 提示语)。"""
        if self.wallet.water <= 0:
            return False, "水也不多了，继续听歌赚水吧"
        self.wallet.water -= 1
        self.mood.sleepiness = max(0.0, self.mood.sleepiness - 18.0)
        self.mood.hunger = max(0.0, self.mood.hunger - 8.0)
        return True, "喝饱水啦！"

    # ------------------------------------------------------------ speech

    def say(self, kind: str) -> str:
        lines = SAYINGS.get(kind, ())
        self._speech = lines[int(self._now()) % len(lines)] if lines else ""
        return self._speech

    @property
    def speech(self) -> str:
        return self._speech


def pet_settings_key(kind: str, name: str) -> str:
    return f"pet.{kind}.{name}"
