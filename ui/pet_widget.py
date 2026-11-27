"""桌面宠物挂件窗口（2D 程序化绘制版）。

无边框置顶小窗，QTimer 驱动 PetBrain 决策与自绘动画。
形象 = 品种皮肤（core.pet.BREEDS：毛色/花纹/耳型）+ 统一动作骨架，
全部 QPainter 程序化渲染，无外部素材依赖。

互动：左键单击 = 摸头（开心蹦跳）；睡着时单击 = 硬叫醒（弓背炸毛生气）；
左键拖动 = 挪位置；右键 = 投喂/加水菜单。
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QRectF, QTimer, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QMenu, QWidget

from core.pet import (
    BREEDS,
    DAILY_HOUR_CAP,
    DEFAULT_BREED,
    PetBrain,
    PetState,
)

PAPER = "#F4F0E5"
INK = "#0E111C"
VERMILION = "#F5513A"
BUBBLE_BG = "#FCF9F1"


def _ink_alpha(alpha: int) -> QColor:
    """INK 色带透明度。QColor 不接受 (str, int) 签名，只能先建再 setAlpha。"""
    color = QColor(INK)
    color.setAlpha(max(0, min(255, alpha)))
    return color

# 桌面宠物主体尺寸
PET_SIZE = 128
BOWL_MARGIN = 8


class PetWidget(QWidget):
    """一只住在桌面上的猫/狗（品种皮肤 + 统一动作骨架）。"""

    def __init__(self, brain: PetBrain, parent=None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,           # 不在任务栏出现
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.brain = brain
        self.breed = DEFAULT_BREED          # 品种 id，见 core.pet.BREEDS
        self._phase = 0.0                   # 动画相位（走路摆动/呼吸起伏）
        self._drag_pos: QPoint | None = None
        self._press_pos: QPoint | None = None
        self._pressed_moving = False
        self._bubble_text = ""
        self._bubble_until = 0.0
        self._tick_count = 0
        self._ball_x = 0.5                  # 玩球时小球位置（0..1 相对）

        self.setFixedSize(PET_SIZE + 48, PET_SIZE + 72)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(80)               # ~12.5 fps，够顺滑且省电

    # ------------------------------------------------------------ breed skin

    @property
    def skin(self) -> dict:
        """当前品种的外观参数表（缺省回退橘猫）。"""
        return BREEDS.get(self.breed) or BREEDS[DEFAULT_BREED]

    def bind_persistence(self, saver) -> None:
        """注入保存回调（避免 widget 直接依赖 settings 层）。"""
        self._saver = saver

    def _save(self) -> None:
        saver = getattr(self, "_saver", None)
        if saver:
            try:
                saver()
            except Exception:
                pass

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.show()

    def stop(self) -> None:
        self.hide()

    def _tick(self) -> None:
        self._tick_count += 1
        self._phase += 0.18
        brain = self.brain
        brain.tick_earnings()
        new_state = brain.decide()
        if new_state is not brain.state:
            brain.state = new_state
        if brain.speech and brain.speech != self._bubble_text:
            self._show_bubble(brain.speech, 3.2)
        if self._tick_count % 150 == 0:     # 约 12 秒存一次
            self._save()
        self.update()

    # ------------------------------------------------------------ interactions

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
            self._drag_pos = self._press_pos - self.frameGeometry().topLeft()
            self._pressed_moving = False

    def mouseMoveEvent(self, event) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            if self._press_pos is not None:
                delta = event.globalPosition().toPoint() - self._press_pos
                if delta.manhattanLength() > 8:
                    self._pressed_moving = True

    def mouseReleaseEvent(self, event) -> None:
        was_click = (
            not self._pressed_moving
            and self._press_pos is not None
            and event.button() == Qt.MouseButton.LeftButton
        )
        self._drag_pos = None
        self._press_pos = None
        if was_click:
            self._handle_click()

    def _handle_click(self) -> None:
        """单击：睡着=硬叫醒（可能弓背生气），醒着=摸头（开心）。"""
        if self.brain.state is PetState.SLEEPING:
            ok, message = self.brain.wake_up()
        else:
            ok, message = self.brain.pet_head()
        self._show_bubble(message, 2.5, error=not ok)
        self._save()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        feed = menu.addAction(f"🍚 投喂猫粮（库存 {self.brain.wallet.food}）")
        water = menu.addAction(f"💧 加水（库存 {self.brain.wallet.water}）")
        menu.addSeparator()
        info = menu.addAction(
            f"今日已听 {self.brain.wallet.earned_seconds_today / 3600:.1f} 小时"
            f"（上限 {DAILY_HOUR_CAP} 小时）"
        )
        info.setEnabled(False)
        chosen = menu.exec(event.globalPos())
        if chosen is feed:
            ok, message = self.brain.feed()
            self._show_bubble(message, 2.5, error=not ok)
            self._save()
        elif chosen is water:
            ok, message = self.brain.give_water()
            self._show_bubble(message, 2.5, error=not ok)
            self._save()

    def _show_bubble(self, text: str, seconds: float, error: bool = False) -> None:
        import time as _time

        self._bubble_text = text
        self._bubble_error = error
        self._bubble_until = _time.time() + seconds

    # ------------------------------------------------------------ painting

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()

        ground_y = h - 26
        self._paint_bowls(p, ground_y)
        body_rect = QRectF(24, 10, PET_SIZE, PET_SIZE)
        self._paint_pet(p, body_rect)
        self._paint_ball(p, ground_y)
        self._paint_bubble(p)

    # -- 身体（统一骨架） ---------------------------------------------------

    def _paint_pet(self, p: QPainter, rect: QRectF) -> None:
        state = self.brain.state
        cx = rect.center().x()

        # 各状态的躯干姿态
        if state is PetState.SLEEPING:
            self._pose_sleep(p, rect)
            return
        if state is PetState.STRETCHING:
            self._pose_stretch(p, rect)
            return
        if state is PetState.ANGRY:
            self._pose_angry(p, rect)
            return

        bounce = math.sin(self._phase * (2.2 if state is PetState.PARKOUR else 1.1)) * 3.0
        if state is PetState.HAPPY:
            # 开心：原地小蹦跳，幅度更大
            bounce = math.sin(self._phase * 4.0) * 9.0
        lift = -abs(bounce) if state in (
            PetState.PLAYING, PetState.PARKOUR, PetState.HAPPY
        ) else bounce * 0.4
        body = rect.translated(0, lift)
        self._draw_body(p, body)
        self._draw_face(p, body)

    def _pose_sleep(self, p: QPainter, rect: QRectF) -> None:
        body = rect.translated(0, 26)
        self._draw_body(p, body, squashed=True)
        self._draw_face(p, body, sleeping=True)
        self._draw_zzz(p, rect.topRight())

    def _pose_stretch(self, p: QPainter, rect: QRectF) -> None:
        """伸懒腰：前身压低前爪前伸，屁股撅起（猫式伸展）。"""
        ease = (math.sin(self._phase * 1.6) + 1) / 2      # 0..1 呼吸式起伏
        body = rect.translated(4, 18 + ease * 4)
        self._draw_body(p, body, squashed=True, stretch=True)
        self._draw_face(p, body)
        self._draw_stretch_label(p, rect)

    def _pose_angry(self, p: QPainter, rect: QRectF) -> None:
        """弓背炸毛：背部隆起，尾巴竖直放大，眉毛倒竖。"""
        shake = math.sin(self._phase * 6.0) * 2.0
        body = rect.translated(shake, 14)
        self._draw_body(p, body, arched=True)
        self._draw_face(p, body, angry=True)
        self._draw_anger_marks(p, rect)

    def _draw_body(
        self,
        p: QPainter,
        rect: QRectF,
        squashed: bool = False,
        stretch: bool = False,
        arched: bool = False,
    ) -> None:
        """Q 版体态：大头 + 椭圆身 + 耳朵/尾巴由品种皮肤决定。"""
        skin = self.skin
        body_color = QColor(skin["body"])
        belly = QColor(skin["belly"])
        stroke = QPen(QColor(INK), 2.5)
        stroke.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

        body_h = rect.height() * (0.62 if squashed else 0.55)
        body = QRectF(rect.left(), rect.top() + rect.height() - body_h, rect.width(), body_h)
        p.setPen(stroke)
        p.setBrush(QBrush(body_color))
        p.drawEllipse(body)

        # 肚皮浅色
        p.setBrush(QBrush(belly))
        p.setPen(Qt.PenStyle.NoPen)
        belly_h = body_h * 0.55
        p.drawEllipse(QRectF(
            body.center().x() - body.width() * 0.30,
            body.bottom() - belly_h - 4,
            body.width() * 0.60,
            belly_h,
        ))

        # 头（弓背时头压低，伸懒腰时头前伸）
        head_size = rect.width() * 0.72
        head_dx = 10 if stretch else (-4 if arched else 0)
        head = QRectF(
            rect.center().x() - head_size / 2 + head_dx,
            rect.top() + (6 if squashed else 0) + (10 if arched else 0),
            head_size,
            head_size * 0.88,
        )
        p.setPen(stroke)
        p.setBrush(QBrush(body_color))
        p.drawEllipse(head)

        # 花纹（画在头/身体之后、五官之前）
        self._draw_pattern(p, head, body, arched=arched)

        # 耳朵（先画耳再画脸内圈，避免被头盖住的关键轮廓用描边补）
        self._draw_ears(p, head, body_color)

        # 尾巴
        self._draw_tail(p, body, arched=arched)

        self._head_rect = head
        self._body_rect = body

    def _draw_ears(self, p: QPainter, head: QRectF, body_color: QColor) -> None:
        skin = self.skin
        ear_kind = skin.get("ear", "pointed")
        inner = QColor(skin["belly"])
        if ear_kind == "floppy":
            # 狗：外层垂耳
            p.setBrush(QBrush(body_color.darker(108)))
            for sign in (-1, 1):
                ear = QRectF(
                    head.center().x() + sign * head.width() * 0.36 - 11,
                    head.top() - 4,
                    22,
                    26,
                )
                p.drawEllipse(ear)
            p.setBrush(QBrush(QColor(skin["pattern_color"]).darker(112)))
            for sign in (-1, 1):
                ear = QRectF(
                    head.center().x() + sign * head.width() * 0.36 - 7,
                    head.top() + 2,
                    14,
                    18,
                )
                p.drawEllipse(ear)
            return
        # 猫：三角耳；德文=大低耳
        scale = 1.35 if ear_kind == "big" else 1.0
        offset = 2 if ear_kind == "big" else 0
        p.setBrush(QBrush(body_color))
        for sign in (-1, 1):
            base_x = head.center().x() + sign * head.width() * 0.30
            ear = QPainterPath()
            ear.moveTo(base_x - sign * 8, head.top() + 8 + offset)
            ear.lineTo(base_x + sign * 6, head.top() - 16 * scale + offset)
            ear.lineTo(base_x + sign * 16, head.top() + 12 + offset)
            ear.closeSubpath()
            p.drawPath(ear)
        # 耳内粉
        p.setBrush(QBrush(inner if ear_kind != "big" else QColor("#E8B7B0")))
        for sign in (-1, 1):
            base_x = head.center().x() + sign * head.width() * 0.30
            ear = QPainterPath()
            ear.moveTo(base_x - sign * 4, head.top() + 9 + offset)
            ear.lineTo(base_x + sign * 4, head.top() - 8 * scale + offset)
            ear.lineTo(base_x + sign * 10, head.top() + 10 + offset)
            ear.closeSubpath()
            p.drawPath(ear)

    def _draw_pattern(
        self, p: QPainter, head: QRectF, body: QRectF, arched: bool = False
    ) -> None:
        """品种花纹：tabby 条纹 / calico 斑块 / point 重点色 / shaded 渐层。"""
        skin = self.skin
        kind = skin.get("pattern", "solid")
        if kind == "solid":
            return
        pc = QColor(skin["pattern_color"])
        p.setPen(Qt.PenStyle.NoPen)

        if kind == "tabby":
            color = pc if arched is False else pc
            # 头顶川字纹
            p.setBrush(QBrush(color))
            hx = head.center().x()
            for i, dy in enumerate((-2, 2, 6)):
                w = 4 if i == 1 else 3
                p.drawRoundedRect(
                    QRectF(hx - w / 2 + (i - 1) * 7, head.top() + 2, w, 12 + dy), 2, 2
                )
            # 身体两侧虎斑
            for sign in (-1, 1):
                for i in range(3):
                    bx = body.center().x() + sign * (10 + i * 9)
                    p.drawRoundedRect(
                        QRectF(bx - 2, body.top() + 4 + i * 5, 4, body.height() * 0.42),
                        2, 2,
                    )
        elif kind == "calico":
            # 头部两块色斑（一橘一深），身上一大块
            p.setBrush(QBrush(pc))
            p.drawEllipse(QRectF(head.left() + 2, head.top() + 4, head.width() * 0.42, head.height() * 0.5))
            dark = QColor("#4E3B28")
            p.setBrush(QBrush(dark))
            p.drawEllipse(QRectF(head.right() - head.width() * 0.40, head.top() + 6, head.width() * 0.34, head.height() * 0.42))
            p.setBrush(QBrush(pc))
            p.drawEllipse(QRectF(body.left() + 4, body.top() + 6, body.width() * 0.5, body.height() * 0.6))
        elif kind == "point":
            # 重点色：耳/面罩/尾深色
            p.setBrush(QBrush(pc))
            for sign in (-1, 1):
                base_x = head.center().x() + sign * head.width() * 0.30
                ear = QPainterPath()
                ear.moveTo(base_x - sign * 6, head.top() + 7)
                ear.lineTo(base_x + sign * 4, head.top() - 11)
                ear.lineTo(base_x + sign * 13, head.top() + 11)
                ear.closeSubpath()
                p.drawPath(ear)
            p.drawEllipse(QRectF(
                head.center().x() - head.width() * 0.16,
                head.bottom() - head.height() * 0.30,
                head.width() * 0.32,
                head.height() * 0.22,
            ))
        elif kind == "shaded":
            # 渐层：身体/头顶部一层深色罩（上深下浅）
            grad = QRadialGradient(head.center().x(), head.top(), head.width() * 0.9)
            grad.setColorAt(0.0, pc)
            grad.setColorAt(0.55, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(grad))
            p.drawEllipse(head)
            grad2 = QRadialGradient(body.center().x(), body.top(), body.width())
            grad2.setColorAt(0.0, pc)
            grad2.setColorAt(0.6, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(grad2))
            p.drawEllipse(body)

    def _draw_tail(self, p: QPainter, body: QRectF, arched: bool = False) -> None:
        skin = self.skin
        pc = QColor(skin["pattern_color"])
        if arched:
            # 炸毛尾巴：竖直粗尾 + 锯齿边
            wag = math.sin(self._phase * 9.0) * 3
            tail = QPainterPath()
            tail.moveTo(body.right() - 6, body.center().y())
            tail.cubicTo(
                body.right() + 22, body.center().y() - 14 + wag,
                body.right() + 14, body.top() - 26 + wag,
                body.right() - 2, body.top() - 34 + wag,
            )
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(pc, 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPath(tail)
            p.setPen(QPen(_ink_alpha(180), 2))
            p.drawPath(tail)
            return
        wag = math.sin(self._phase * 3.0) * 10
        tail = QPainterPath()
        tail.moveTo(body.right() - 10, body.center().y())
        tail.cubicTo(
            body.right() + 26, body.center().y() - 18 + wag,
            body.right() + 30, body.top() - 10 + wag,
            body.right() + 8, body.top() - 20 + wag,
        )
        p.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(QColor(INK), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawPath(tail)

    # -- 五官 -------------------------------------------------------------

    def _draw_face(
        self,
        p: QPainter,
        rect: QRectF,
        sleeping: bool = False,
        angry: bool = False,
    ) -> None:
        skin = self.skin
        head = getattr(self, "_head_rect", None) or rect
        eye_y = head.center().y() - head.height() * 0.05
        dx = head.width() * 0.18
        happy = self.brain.state is PetState.HAPPY
        p.setPen(Qt.PenStyle.NoPen)

        if sleeping:
            p.setPen(QPen(QColor(INK), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for sign in (-1, 1):
                x = head.center().x() + sign * dx
                p.drawLine(int(x - 6), int(eye_y), int(x + 6), int(eye_y))
        elif angry:
            # 怒目圆睁 + 倒竖眉
            p.setBrush(QBrush(QColor(INK)))
            for sign in (-1, 1):
                x = head.center().x() + sign * dx
                p.drawEllipse(QRectF(x - 5, eye_y - 5, 10, 10))
            p.setPen(QPen(QColor(INK), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for sign in (-1, 1):
                x = head.center().x() + sign * dx
                p.drawLine(
                    int(x - sign * 7), int(eye_y - 12),
                    int(x + sign * 5), int(eye_y - 7),
                )
        elif happy:
            # 开心眯眯眼 ^ ^（两段折线）
            p.setPen(QPen(QColor(INK), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for sign in (-1, 1):
                x = head.center().x() + sign * dx
                p.drawLine(int(x - 6), int(eye_y + 2), int(x), int(eye_y - 4))
                p.drawLine(int(x), int(eye_y - 4), int(x + 6), int(eye_y + 2))
        else:
            blink = (int(self._phase * 10) % 47) == 0
            p.setBrush(QBrush(QColor(INK)))
            if blink:
                p.setPen(QPen(QColor(INK), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                for sign in (-1, 1):
                    x = head.center().x() + sign * dx
                    p.drawLine(int(x - 5), int(eye_y), int(x + 5), int(eye_y))
            else:
                eye_color = QColor("#5B8DB8") if skin.get("blue_eyes") else QColor(INK)
                for sign in (-1, 1):
                    x = head.center().x() + sign * dx
                    p.setBrush(QBrush(eye_color))
                    p.drawEllipse(QRectF(x - 5, eye_y - 6, 10, 12))
                p.setBrush(QBrush(QColor("white")))
                for sign in (-1, 1):
                    x = head.center().x() + sign * dx
                    p.drawEllipse(QRectF(x - 2, eye_y - 4, 4, 4))

        # 鼻+嘴（生气时嘴巴变波浪撇嘴）
        p.setBrush(QBrush(QColor(VERMILION)))
        nose_y = eye_y + head.height() * 0.16
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(head.center().x() - 3.5, nose_y - 3.5, 7, 6))
        p.setPen(QPen(QColor(INK), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        mouth = QPainterPath()
        if angry:
            mouth.moveTo(head.center().x() - 8, nose_y + 12)
            mouth.quadTo(head.center().x(), nose_y + 5, head.center().x() + 8, nose_y + 12)
        else:
            mouth.moveTo(head.center().x() - 8, nose_y + 8)
            mouth.quadTo(head.center().x(), nose_y + 15, head.center().x() + 8, nose_y + 8)
        p.drawPath(mouth)
        # 胡须（猫科；生气时炸开更斜）
        if skin.get("ear", "pointed") != "floppy":
            spread = 46 if angry else 34
            drop = 12 if angry else 7
            p.setPen(QPen(QColor(90, 90, 90, 160), 1.4))
            for sign in (-1, 1):
                for dy in (-3, 2, 7):
                    p.drawLine(
                        int(head.center().x() + sign * 14), int(nose_y + 4),
                        int(head.center().x() + sign * spread), int(nose_y + 4 + dy * (2 if angry else 1) - (drop if angry else 0)),
                    )

    def _draw_anger_marks(self, p: QPainter, rect: QRectF) -> None:
        """生气符号：头顶青筋十字 + 身边炸毛短线。"""
        p.setPen(QPen(QColor("#D6453A"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        cx, cy = rect.right() - 6, rect.top() + 6
        p.drawLine(int(cx - 7), int(cy - 7), int(cx + 1), int(cy + 1))
        p.drawLine(int(cx + 1), int(cy - 7), int(cx - 7), int(cy + 1))
        p.drawLine(int(cx - 3), int(cy - 9), int(cx + 5), int(cy - 1))
        # 炸毛短线
        body = getattr(self, "_body_rect", None)
        if body is not None:
            p.setPen(QPen(_ink_alpha(150), 1.6))
            for i in range(6):
                ang = math.pi * (0.15 + i * 0.14)
                bx = body.center().x() + math.cos(ang) * body.width() * 0.55
                by = body.center().y() - math.sin(ang) * body.height() * 0.55
                p.drawLine(int(bx), int(by), int(bx + math.cos(ang) * 8), int(by - math.sin(ang) * 8))

    def _draw_stretch_label(self, p: QPainter, rect: QRectF) -> None:
        p.setPen(QPen(_ink_alpha(140), 1.8))
        p.drawText(int(rect.right()) - 30, int(rect.top()) + 12, "～")

    def _draw_zzz(self, p: QPainter, top_right) -> None:
        import time as _time
        p.setPen(QPen(_ink_alpha(200), 2))
        font = p.font()
        font.setPointSize(10 + (int(_time.time() * 2) % 3))
        p.setFont(font)
        drift = math.sin(_time.time() * 1.5) * 4
        p.drawText(int(top_right.x()) - 4, int(top_right.y()) - 6 + drift, "z z Z")

    # -- 饭盆水盆 ---------------------------------------------------------

    def _paint_bowls(self, p: QPainter, ground_y: float) -> None:
        brain = self.brain
        bowl_w, bowl_h = 34, 14
        food_empty = brain.wallet.food <= 0
        water_empty = brain.wallet.water <= 0

        for index, (label, empty, color, fill_ratio) in enumerate(
            (
                ("粮", food_empty, QColor("#D98A4A"), min(1.0, brain.wallet.food / 3)),
                ("水", water_empty, QColor("#6FA8DC"), min(1.0, brain.wallet.water / 6)),
            )
        ):
            x = 30 + index * (bowl_w + 26)
            y = ground_y - bowl_h
            p.setPen(QPen(QColor(INK), 2))
            p.setBrush(QBrush(QColor(BUBBLE_BG)))
            p.drawRoundedRect(QRectF(x, y, bowl_w, bowl_h), 6, 6)
            if not empty and fill_ratio > 0:
                p.setBrush(QBrush(color))
                p.setPen(Qt.PenStyle.NoPen)
                inner_h = (bowl_h - 6) * fill_ratio
                p.drawRoundedRect(
                    QRectF(x + 3, y + bowl_h - 3 - inner_h, bowl_w - 6, inner_h), 3, 3
                )
            p.setPen(QPen(_ink_alpha(170), 1))
            f = p.font()
            f.setPointSize(7)
            p.setFont(f)
            p.drawText(QRectF(x - 6, y - 14, bowl_w + 12, 12),
                       Qt.AlignmentFlag.AlignCenter, label)

    # -- 玩球 -------------------------------------------------------------

    def _paint_ball(self, p: QPainter, ground_y: float) -> None:
        if self.brain.state is not PetState.PLAYING:
            return
        t = (math.sin(self._phase * 2.0) + 1) / 2
        x = 40 + t * (self.width() - 110)
        hop = abs(math.sin(self._phase * 4.0)) * 26
        p.setBrush(QBrush(QColor(VERMILION)))
        p.setPen(QPen(QColor("#A32D2D"), 1.5))
        p.drawEllipse(QRectF(x, ground_y - 12 - hop, 16, 16))
        p.setPen(QPen(QColor("white"), 2))
        p.drawLine(int(x + 3), int(ground_y - 4 - hop), int(x + 13), int(ground_y - 12 - hop))

    # -- 气泡 -------------------------------------------------------------

    def _paint_bubble(self, p: QPainter) -> None:
        import time as _time

        text = self._bubble_text
        if not text or _time.time() > self._bubble_until:
            return
        p.setFont(self.font())
        metrics = p.fontMetrics()
        text_w = metrics.horizontalAdvance(text)
        pad = 10
        box_w = text_w + pad * 2
        box_h = metrics.height() + 12
        head = getattr(self, "_head_rect", None)
        bx = (self.width() - box_w) / 2
        by = 2
        bubble = QRectF(bx, by, box_w, box_h)

        p.setPen(QPen(QColor(INK), 1.6))
        p.setBrush(QBrush(QColor("#FCE8E6" if getattr(self, "_bubble_error", False) else BUBBLE_BG)))
        p.drawRoundedRect(bubble, 9, 9)
        # 尝试往头顶指的小三角
        tail = QPainterPath()
        cx = (head.center().x() if head else self.width() / 2)
        tail.moveTo(cx - 6, bubble.bottom() - 1)
        tail.lineTo(cx, bubble.bottom() + 8)
        tail.lineTo(cx + 6, bubble.bottom() - 1)
        p.drawPolygon([tail.elementAt(0), tail.elementAt(1), tail.elementAt(2)])
        p.setPen(QColor(INK))
        p.drawText(bubble, Qt.AlignmentFlag.AlignCenter, text)
