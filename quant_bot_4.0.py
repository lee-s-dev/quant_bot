import os
import sys
import time
import json
import html
import datetime
import threading
import logging
import queue
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import pyupbit
import ta
import telebot
import psutil
from dotenv import load_dotenv

# ── HTTP 전역 타임아웃 패치 (ConnectTimeout=None 방지) ──────────────────────
_orig_request = requests.Session.request
def _patched_request(self, *args, **kwargs):
    kwargs.setdefault("timeout", 10)
    return _orig_request(self, *args, **kwargs)
requests.Session.request = _patched_request
# ───────────────────────────────────────────────────────────────────────────


def setup_logger():
    logger = logging.getLogger("QuantBot")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(message)s")

    file_handler = RotatingFileHandler(
        "quant_bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    if not logger.handlers:
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
    return logger


logger = setup_logger()

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(env_path)

ACCESS_KEY = os.getenv("UPBIT_ACCESS_KEY")
SECRET_KEY = os.getenv("UPBIT_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TICKER = "KRW-BTC"
KST = ZoneInfo("Asia/Seoul")

# ── 전략 파라미터 (walkforward.py 검증 결과 기반 — strategy.md 참고) ──────
ADX_MIN     = 28      # 1h ADX 추세 강도 하한
HIGHEST_N   = 60      # 15m 돌파 기준 최고가 봉 수
ATR_MIN_PCT = 0.0015  # 최소 변동성 (ATR/가격)
RSI_MAX     = 76      # 15m RSI 과매수 차단
VOL_MULT    = 1.3     # 진입봉 거래량 > 20봉 평균 × 배수
BLOCK_HOUR_START, BLOCK_HOUR_END = 1, 4  # KST 진입 차단 시간대 (유동성 저조)
SL_ATR      = 1.5     # 손절: 진입가 - ATR 배수
TP1_ATR     = 2.0     # 1차 익절: 진입가 + ATR 배수
TP1_RATIO   = 0.4     # 1차 익절 매도 비율
TP2_ATR     = 6.0     # 2차 익절: 진입가 + ATR 배수
TRAIL_ATR   = 1.2     # TP1 이후 트레일 스탑: 진입가 + ATR 배수
FEE_RATE    = 0.0005  # 업비트 단방향 수수료 0.05%

STATE_FILE = Path(__file__).resolve().parent / "bot_state.json"
FNG_URL = "https://api.alternative.me/fng/?limit=1"

if not ACCESS_KEY or not SECRET_KEY or not TELEGRAM_TOKEN or not str(CHAT_ID or "").strip():
    logger.error("🚨 .env 확인: 키/토큰/채팅ID 누락")
    sys.exit(1)


# ── 포맷 헬퍼 ──────────────────────────────────────────────────────────────
def esc(s):
    """HTML 특수문자 이스케이프 (parse_mode=HTML 사용 시 필수)."""
    if s is None:
        return ""
    return html.escape(str(s), quote=False)


def b(text):
    """HTML bold 래퍼."""
    return f"<b>{esc(text)}</b>"


def fmt_krw(v):
    if v is None:
        return "N/A"
    return f"{v:,.0f}원"


def fmt_krw_short(v):
    """괄호용 축약 표기: 약 X.X만원 / 약 X.XX억원."""
    if v is None:
        return "N/A"
    av = abs(v)
    sign = "-" if v < 0 else ""
    if av >= 100_000_000:
        return f"{sign}약 {av/100_000_000:.2f}억원"
    if av >= 10_000:
        return f"{sign}약 {av/10_000:.1f}만원"
    return f"{v:,.0f}원"


def fmt_pct(v, signed=True):
    if v is None:
        return "N/A"
    sign = ("+" if v >= 0 else "")
    if not signed:
        sign = ""
    return f"{sign}{v:.2f}%"


def fmt_pnl_icon(v):
    if v is None or v == 0:
        return "📈" if (v or 0) >= 0 else "📉"
    return "📈" if v > 0 else "📉"


def fmt_pnl(v):
    """확정 손익 라벨 (굵게)."""
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    icon = "📈" if v >= 0 else "📉"
    return f"<b>{sign}{v:,.0f}원</b> {icon}"


def now_kst():
    return datetime.datetime.now(KST)


def dt_to_iso(dt):
    if not isinstance(dt, datetime.datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.isoformat()


def parse_kst_dt(value, default=None):
    if not value:
        return default if default is not None else now_kst()
    try:
        dt = datetime.datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except Exception:
        return default if default is not None else now_kst()


def calc_realized_pnl(fill_price, entry_price, volume):
    """수수료 반영 실현손익. 매수·매도 수수료를 모두 차감한 순손익."""
    if not (fill_price and entry_price and volume):
        return 0.0
    return volume * (fill_price * (1 - FEE_RATE) - entry_price * (1 + FEE_RATE))


def fetch_fear_greed():
    try:
        r = requests.get(FNG_URL, timeout=5)
        data = r.json()
        val = data["data"][0]["value"]
        cls = data["data"][0]["value_classification"]
        return f"{val} ({cls})"
    except Exception as e:
        logger.warning(f"공포/탐욕 지수 조회 실패: {e}")
        return "N/A"


# ── 데이터 / 도메인 클래스 ─────────────────────────────────────────────────
class DataProvider:
    @staticmethod
    def get_market_data():
        try:
            df_1h = pyupbit.get_ohlcv(TICKER, interval="minute60", count=100)
            df_15m = pyupbit.get_ohlcv(TICKER, interval="minute15", count=120)
            if df_1h is None or df_15m is None:
                return None, None

            df_1h["ema20"] = ta.trend.EMAIndicator(df_1h["close"], window=20).ema_indicator()
            df_1h["ema50"] = ta.trend.EMAIndicator(df_1h["close"], window=50).ema_indicator()
            df_1h["adx"] = ta.trend.ADXIndicator(
                df_1h["high"], df_1h["low"], df_1h["close"], window=14
            ).adx()

            df_15m["atr"] = ta.volatility.AverageTrueRange(
                df_15m["high"], df_15m["low"], df_15m["close"], window=14
            ).average_true_range()
            df_15m["rsi"] = ta.momentum.RSIIndicator(
                df_15m["close"], window=14
            ).rsi()
            df_15m["vol_ma20"] = df_15m["volume"].rolling(window=20).mean()
            df_15m["highest"] = df_15m["high"].shift(1).rolling(window=HIGHEST_N).max()

            return df_1h.iloc[-1], df_15m
        except Exception as e:
            logger.error(f"데이터 수집 에러: {e}")
            return None, None

    @staticmethod
    def is_flash_crash_active():
        try:
            df_5m = pyupbit.get_ohlcv(TICKER, interval="minute5", count=3)
            if df_5m is None:
                return False
            drop_rate = (df_5m["close"].iloc[-1] / df_5m["close"].iloc[-2] - 1) * 100
            return drop_rate <= -5.0
        except Exception as e:
            logger.warning(f"서킷브레이커 감시 에러: {e}")
            return False

    @staticmethod
    def get_market_snapshot():
        """/market 용 스냅샷. 실패 항목은 None."""
        snap = {
            "price": None, "change_pct_24h": None, "volume_24h_krw": None,
            "rsi_15m": None, "atr_15m": None,
            "ema20_1h": None, "ema50_1h": None, "adx_1h": None,
            "fng": None,
        }
        try:
            df_day = pyupbit.get_ohlcv(TICKER, interval="day", count=2)
            if df_day is not None and len(df_day) >= 2:
                prev_close = df_day["close"].iloc[-2]
                now_close = df_day["close"].iloc[-1]
                snap["price"] = float(now_close)
                snap["change_pct_24h"] = (now_close / prev_close - 1) * 100
                snap["volume_24h_krw"] = float(df_day["value"].iloc[-1])
        except Exception as e:
            logger.warning(f"일봉 스냅샷 실패: {e}")

        try:
            df_15m = pyupbit.get_ohlcv(TICKER, interval="minute15", count=50)
            if df_15m is not None:
                rsi = ta.momentum.RSIIndicator(df_15m["close"], window=14).rsi()
                atr = ta.volatility.AverageTrueRange(
                    df_15m["high"], df_15m["low"], df_15m["close"], window=14
                ).average_true_range()
                snap["rsi_15m"] = float(rsi.iloc[-1])
                snap["atr_15m"] = float(atr.iloc[-1])
        except Exception as e:
            logger.warning(f"15분봉 스냅샷 실패: {e}")

        try:
            df_1h = pyupbit.get_ohlcv(TICKER, interval="minute60", count=100)
            if df_1h is not None:
                ema20 = ta.trend.EMAIndicator(df_1h["close"], window=20).ema_indicator()
                ema50 = ta.trend.EMAIndicator(df_1h["close"], window=50).ema_indicator()
                adx = ta.trend.ADXIndicator(
                    df_1h["high"], df_1h["low"], df_1h["close"], window=14
                ).adx()
                snap["ema20_1h"] = float(ema20.iloc[-1])
                snap["ema50_1h"] = float(ema50.iloc[-1])
                snap["adx_1h"] = float(adx.iloc[-1])
        except Exception as e:
            logger.warning(f"1시간봉 스냅샷 실패: {e}")

        snap["fng"] = fetch_fear_greed()
        return snap


class Position:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.state = "IDLE"
            self.entry_price = 0.0
            self.atr_at_entry = 0.0
            self.breakout_price = 0.0
            self.tp1_done = False

    def enter_setup(self, breakout_price):
        with self.lock:
            self.state = "SETUP"
            self.breakout_price = breakout_price

    def enter_position(self, filled_price, atr):
        with self.lock:
            self.state = "POSITION"
            self.entry_price = filled_price
            self.atr_at_entry = atr
            self.tp1_done = False

    def sync_to_existing_position(self, avg_buy_price):
        with self.lock:
            self.state = "POSITION"
            self.entry_price = avg_buy_price
            self.tp1_done = False

    def restore(self, data):
        with self.lock:
            self.state = data.get("state", "IDLE")
            self.entry_price = float(data.get("entry_price", 0.0) or 0.0)
            self.atr_at_entry = float(data.get("atr_at_entry", 0.0) or 0.0)
            self.breakout_price = float(data.get("breakout_price", 0.0) or 0.0)
            self.tp1_done = bool(data.get("tp1_done", False))

    def snapshot(self):
        with self.lock:
            return {
                "state": self.state,
                "entry_price": self.entry_price,
                "atr_at_entry": self.atr_at_entry,
                "breakout_price": self.breakout_price,
                "tp1_done": self.tp1_done,
            }


class RiskManager:
    @staticmethod
    def get_position_size(krw_balance, current_price, atr_value):
        if atr_value <= 0 or current_price <= 0:
            return 0.0
        risk_amount = krw_balance * 0.01
        stop_loss_pct = (SL_ATR * atr_value) / current_price
        return min(risk_amount / stop_loss_pct, krw_balance * 0.5)


class Execution:
    def __init__(self, upbit_client):
        self.upbit = upbit_client

    def safe_market_buy(self, ticker, budget):
        try:
            order = self.upbit.buy_market_order(ticker, budget * 0.9995)
            if order is None or (isinstance(order, dict) and "error" in order):
                logger.error(f"매수 API 거절: {order}")
                return None
            if isinstance(order, list) and len(order) > 0:
                return order[0].get("uuid")
            if isinstance(order, dict):
                return order.get("uuid")
            return None
        except Exception as e:
            logger.error(f"매수 실행 에러: {e}")
            return None

    def safe_market_sell(self, ticker, volume_ratio=1.0):
        """매도 실행. 반환: (성공 여부, 체결 추정 수량, 주문 uuid)."""
        try:
            initial_volume = self.upbit.get_balance(ticker)
            if initial_volume is None:
                logger.error("매도 잔고 조회 실패 (None 반환) — 매도 취소")
                return False, 0.0, None
            sell_volume = round(initial_volume * volume_ratio, 8)
            if sell_volume <= 0:
                return False, 0.0, None

            order = self.upbit.sell_market_order(ticker, sell_volume)
            if order is None or (isinstance(order, dict) and "error" in order):
                logger.error(f"매도 API 거절: {order}")
                return False, 0.0, None
            uuid = order.get("uuid") if isinstance(order, dict) else None

            for _ in range(5):
                time.sleep(1)
                current_volume = self.upbit.get_balance(ticker)
                if current_volume is None:
                    continue
                expected_remaining = max(0.0, initial_volume - sell_volume)
                tolerance = max(initial_volume * 0.01, sell_volume * 0.05, 1e-8)
                if current_volume <= expected_remaining + tolerance:
                    return True, sell_volume, uuid
            logger.warning("체결 확인 실패: 잔고 변동 부족")
            return False, 0.0, uuid
        except Exception as e:
            logger.error(f"매도 실행 에러: {e}")
            return False, 0.0, None

    def get_avg_fill_price(self, uuid, fallback_price):
        """주문 체결 내역 기반 평균 체결가. 조회 실패 시 fallback 가격."""
        if not uuid:
            return fallback_price
        try:
            for _ in range(3):
                od = self.upbit.get_order(uuid)
                trades = od.get("trades") if isinstance(od, dict) else None
                if trades:
                    vol = sum(float(t["volume"]) for t in trades)
                    funds = sum(float(t["funds"]) for t in trades)
                    if vol > 0:
                        return funds / vol
                time.sleep(0.7)
        except Exception as e:
            logger.warning(f"체결가 조회 실패({uuid}): {e}")
        return fallback_price


# ── 텔레그램 ────────────────────────────────────────────────────────────────
class TelegramManager:
    PARSE_MODE = "HTML"

    def __init__(self, token, chat_id, strategy_instance):
        self.bot = telebot.TeleBot(token)
        self.chat_id = str(chat_id).strip()
        self.strategy = strategy_instance

        # /all, /help, /start
        @self.bot.message_handler(commands=["all", "help", "start"])
        def send_help(message):
            if str(message.chat.id) != self.chat_id: return
            help_text = (
                f"⚙️ {b('QuantBot 명령어')}\n\n"
                f"📊 {b('조회')}\n"
                "▫️ /status — 금고(자산·비중·수익률)\n"
                "▫️ /balance — 잔고 상세\n"
                "▫️ /market — 시장 스캐닝\n"
                "▫️ /history — 최근 거래 내역\n\n"
                f"🎛 {b('통제')}\n"
                "▫️ /pause — 신규 매수 일시정지\n"
                "▫️ /resume — 신규 매수 재개\n"
                "▫️ /panic — 긴급 전량매도 + 대기\n"
                "▫️ /sell_all — 전량매도만 실행\n"
                "▫️ /stop — 봇 프로세스 종료\n\n"
                "❓ /all — 이 목록 다시 보기"
            )
            self._reply(message, help_text)

        # /status
        @self.bot.message_handler(commands=["status"])
        def send_status(message):
            if str(message.chat.id) != self.chat_id: return
            try:
                self._reply(message, self.strategy.build_status_text())
            except Exception as e:
                logger.error(f"/status 핸들러 에러: {e}")
                self._reply(message, f"⚠️ 상태 조회 에러: {esc(e)}")

        # /balance
        @self.bot.message_handler(commands=["balance"])
        def send_balance(message):
            if str(message.chat.id) != self.chat_id: return
            try:
                krw = self.strategy.upbit.get_balance("KRW")
                btc = self.strategy.upbit.get_balance(TICKER)
                price = self.strategy.current_price
                if krw is None or btc is None:
                    self._reply(message, "⚠️ 잔고 조회 실패 (API 오류). 잠시 후 다시 시도하세요.")
                    return
                btc_value = btc * price if price else 0.0
                total = krw + btc_value
                cash_ratio = (krw / total * 100) if total > 0 else 0.0
                msg = (
                    f"💰 {b('잔고 상세')}\n\n"
                    f"▫️ KRW: <b>{fmt_krw(krw)}</b>\n"
                    f"▫️ BTC: {btc:.8f} ({fmt_krw_short(btc_value)})\n"
                    f"▫️ 총자산: <b>{fmt_krw(total)}</b>\n"
                    f"▫️ 현금 비중: <b>{fmt_pct(cash_ratio, signed=False)}</b>"
                )
                self._reply(message, msg)
            except Exception as e:
                logger.error(f"/balance 핸들러 에러: {e}")
                self._reply(message, f"⚠️ 잔고 조회 에러: {esc(e)}")

        # /market
        @self.bot.message_handler(commands=["market"])
        def send_market(message):
            if str(message.chat.id) != self.chat_id: return
            try:
                self._reply(message, self.strategy.build_market_text())
            except Exception as e:
                logger.error(f"/market 핸들러 에러: {e}")
                self._reply(message, f"⚠️ 시장 조회 에러: {esc(e)}")

        # /history
        @self.bot.message_handler(commands=["history"])
        def send_history(message):
            if str(message.chat.id) != self.chat_id: return
            try:
                self._reply(message, self.strategy.build_history_text())
            except Exception as e:
                logger.error(f"/history 핸들러 에러: {e}")
                self._reply(message, f"⚠️ 내역 조회 에러: {esc(e)}")

        # /pause
        @self.bot.message_handler(commands=["pause"])
        def pause_bot(message):
            if str(message.chat.id) != self.chat_id: return
            self.strategy.set_paused(True)
            self.send_msg(
                f"⏸️ {b('일시정지 활성화')}\n\n"
                "▫️ 신규 매수 중단\n"
                "▫️ 기존 포지션 청산 로직은 정상 동작\n"
                "▫️ 재개: /resume"
            )

        # /resume
        @self.bot.message_handler(commands=["resume"])
        def resume_bot(message):
            if str(message.chat.id) != self.chat_id: return
            self.strategy.set_paused(False)
            self.send_msg(
                f"▶️ {b('일시정지 해제')}\n\n"
                "▫️ 신규 매수 재개"
            )

        # /panic
        @self.bot.message_handler(commands=["panic"])
        def panic_handler(message):
            if str(message.chat.id) != self.chat_id: return
            self.send_msg(f"🚨 {b('[PANIC] 긴급 전량매도 실행 중...')}")
            self.strategy.emergency_liquidate(reason="Panic 매도", pause_after=True)

        # /sell_all
        @self.bot.message_handler(commands=["sell_all"])
        def manual_sell_all(message):
            if str(message.chat.id) != self.chat_id: return
            self.send_msg(f"🚨 {b('[사용자 명령] 긴급 전량 매도를 실행합니다.')}")
            self.strategy.emergency_liquidate(reason="사용자 수동 전량매도", pause_after=False)

        # /stop
        @self.bot.message_handler(commands=["stop"])
        def stop_bot(message):
            if str(message.chat.id) != self.chat_id: return
            self.send_msg(f"🛑 {b('봇을 종료합니다.')} 재시작은 서버(PM2)에서 수행해야 합니다.")
            os._exit(0)

    def _reply(self, message, text):
        try:
            self.bot.reply_to(message, text, parse_mode=self.PARSE_MODE)
        except Exception as e:
            logger.error(f"텔레그램 reply 실패 (parse_mode={self.PARSE_MODE}): {e}")
            try:
                self.bot.reply_to(message, text)
            except Exception as e2:
                logger.error(f"텔레그램 reply 재시도 실패: {e2}")

    def send_msg(self, text):
        try:
            self.bot.send_message(self.chat_id, text, parse_mode=self.PARSE_MODE)
            logger.info(f"텔레그램: {text.replace(chr(10), ' | ')}")
        except Exception as e:
            logger.error(f"텔레그램 전송 실패 (parse_mode={self.PARSE_MODE}): {e}")
            try:
                self.bot.send_message(self.chat_id, text)
            except Exception as e2:
                logger.error(f"텔레그램 전송 재시도 실패: {e2}")

    def start_listening(self):
        threading.Thread(target=self.bot.infinity_polling, daemon=True).start()


# ── 전략 / 메인 루프 ───────────────────────────────────────────────────────
class QuantStrategy:
    def __init__(self):
        self.upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)
        self.executor = Execution(self.upbit)
        self.pos = Position()
        self.noti = TelegramManager(TELEGRAM_TOKEN, CHAT_ID, self)

        self.current_price = 0.0
        self.last_ws_update = now_kst()
        self.cooldown_until = now_kst()
        self.flash_crash_until = now_kst()
        self.last_data_fetch = now_kst() - datetime.timedelta(minutes=1)
        self.last_closed_candle_time = None
        self.last_error_time = now_kst() - datetime.timedelta(hours=1)
        self.last_mem_warn = now_kst() - datetime.timedelta(hours=1)
        self.last_mem_check = now_kst() - datetime.timedelta(seconds=60)
        self.wm = None

        self.last_entry_reason = ""

        self.state_lock = threading.RLock()
        self.is_paused_flag = False
        self.trade_history = deque(maxlen=10)
        self.cumulative_pnl = 0.0
        self.previous_day_total = None
        self.last_briefing_date = None
        self._load_state()

        self.init_websocket()
        self.sync_account_on_startup()

    # ── 영구 상태 ──────────────────────────────────────────────────────
    def _load_state(self):
        if not STATE_FILE.exists():
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.cumulative_pnl = float(data.get("cumulative_pnl", 0.0))
            self.previous_day_total = data.get("previous_day_total")
            self.last_briefing_date = data.get("last_briefing_date")
            self.cooldown_until = parse_kst_dt(data.get("cooldown_until"), now_kst())
            self.flash_crash_until = parse_kst_dt(data.get("flash_crash_until"), now_kst())
            self.is_paused_flag = bool(data.get("is_paused", False))
            self.pos.restore(data.get("position", {}))
            for t in data.get("trade_history", [])[-10:]:
                self.trade_history.append(t)
            logger.info(f"상태 파일 로드: 누적 PnL {self.cumulative_pnl:,.0f}원")
        except Exception as e:
            logger.warning(f"상태 파일 로드 실패: {e}")

    def _save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "cumulative_pnl": self.cumulative_pnl,
                    "previous_day_total": self.previous_day_total,
                    "last_briefing_date": self.last_briefing_date,
                    "cooldown_until": dt_to_iso(self.cooldown_until),
                    "flash_crash_until": dt_to_iso(self.flash_crash_until),
                    "is_paused": self.is_paused(),
                    "position": self.pos.snapshot(),
                    "trade_history": list(self.trade_history),
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"상태 파일 저장 실패: {e}")

    # ── 통제 플래그 ────────────────────────────────────────────────────
    def set_paused(self, val: bool):
        with self.state_lock:
            self.is_paused_flag = bool(val)
        logger.info(f"pause 플래그 설정: {val}")
        self._save_state()

    def is_paused(self):
        with self.state_lock:
            return self.is_paused_flag

    # ── 거래 기록 ──────────────────────────────────────────────────────
    def record_trade(self, side, price, volume, reason, pnl=None):
        entry = {
            "time": now_kst().strftime("%Y-%m-%d %H:%M:%S"),
            "side": side,
            "price": float(price) if price else 0.0,
            "volume": float(volume) if volume else 0.0,
            "reason": reason,
            "pnl": (float(pnl) if pnl is not None else None),
        }
        with self.state_lock:
            self.trade_history.append(entry)
            if pnl is not None:
                self.cumulative_pnl += float(pnl)
            self._save_state()

    # ── 웹소켓 ─────────────────────────────────────────────────────────
    def init_websocket(self):
        try:
            if self.wm is not None:
                self.wm.terminate()
                time.sleep(1)
            self.wm = pyupbit.WebSocketManager("ticker", [TICKER])
            self.wm.alive = True
            self.wm.start()
            self.last_ws_update = now_kst()
            logger.info("웹소켓 초기화 성공")
        except Exception as e:
            logger.error(f"웹소켓 초기화 실패: {e}")

    def sync_account_on_startup(self):
        try:
            btc_bal = self.upbit.get_balance(TICKER)
            avg_buy_price = self.upbit.get_avg_buy_price(TICKER)
            if btc_bal and avg_buy_price and (btc_bal * avg_buy_price) > 5000:
                with self.pos.lock:
                    already_restored = self.pos.state == "POSITION" and self.pos.entry_price > 0
                if not already_restored:
                    self.pos.sync_to_existing_position(avg_buy_price)
                    self._save_state()
                logger.info(f"기존 보유 동기화 완료 (평단 {avg_buy_price:,.0f})")
            else:
                with self.pos.lock:
                    stale_state = self.pos.state != "IDLE"
                if stale_state:
                    self.pos.reset()
                    self._save_state()
                    logger.info("실보유 BTC 없음: 저장된 포지션 상태 초기화")
        except Exception as e:
            logger.warning(f"초기 계좌 동기화 실패: {e}")

    def check_websocket_health(self):
        now = now_kst()
        try:
            drained = 0
            latest_price = None

            if self.wm is not None:
                q = getattr(self.wm, "_WebSocketManager__q", None)
                if q is not None:
                    while True:
                        try:
                            data = q.get_nowait()
                            if isinstance(data, dict):
                                trade_price = data.get("trade_price")
                                if trade_price and trade_price > 0:
                                    latest_price = trade_price
                            elif data == "ConnectionClosedError":
                                logger.warning("웹소켓 연결 종료 이벤트 감지")
                            drained += 1
                        except queue.Empty:
                            break

            if latest_price is not None:
                self.current_price = latest_price
                self.last_ws_update = now
                if drained > 1:
                    logger.info(f"웹소켓 큐 드레인: {drained}건 처리")

            if (now - self.last_ws_update).total_seconds() > 30:
                logger.warning("웹소켓 30초 응답 없음. 재연결")
                self.noti.send_msg(f"⚙️ {b('웹소켓 응답 없음 → 재연결 시도')}")
                self.init_websocket()
                fallback = pyupbit.get_current_price(TICKER)
                if fallback:
                    self.current_price = fallback
                    self.last_ws_update = now
        except Exception as e:
            logger.warning(f"웹소켓 헬스체크 에러: {e}")

    # ── 메모리 모니터링 ────────────────────────────────────────────────
    def check_memory(self):
        now = now_kst()
        if (now - self.last_mem_check).total_seconds() < 60:
            return
        self.last_mem_check = now
        try:
            mem = psutil.virtual_memory()
            if mem.percent >= 80 and (now - self.last_mem_warn).total_seconds() >= 3600:
                self.last_mem_warn = now
                self.noti.send_msg(
                    f"🚨 {b('[메모리 위험 수준]')}\n\n"
                    f"▫️ 사용률: <b>{mem.percent:.1f}%</b>\n"
                    f"▫️ 사용량: {mem.used/1024/1024:.0f}MB / {mem.total/1024/1024:.0f}MB\n"
                    f"▫️ PM2 재시작 또는 프로세스 상태 확인 권장"
                )
                logger.warning(f"메모리 경고: {mem.percent:.1f}%")
        except Exception as e:
            logger.warning(f"메모리 체크 에러: {e}")

    # ── 일일 브리핑 ────────────────────────────────────────────────────
    def maybe_send_daily_briefing(self):
        now = now_kst()
        today_str = now.strftime("%Y-%m-%d")
        if now.hour != 9:
            return
        if self.last_briefing_date == today_str:
            return
        try:
            self.send_daily_briefing()
        except Exception as e:
            logger.error(f"일일 브리핑 생성 에러: {e}")
        finally:
            self.last_briefing_date = today_str
            self._save_state()

    def send_daily_briefing(self):
        krw = self.upbit.get_balance("KRW") or 0.0
        btc = self.upbit.get_balance(TICKER) or 0.0
        price = self.current_price or pyupbit.get_current_price(TICKER) or 0.0
        btc_value = btc * price
        total = krw + btc_value

        if self.previous_day_total and self.previous_day_total > 0:
            delta_pct = (total / self.previous_day_total - 1) * 100
            delta_krw = total - self.previous_day_total
            delta_line = f"▫️ 전일 대비: {fmt_pnl(delta_krw)} ({fmt_pct(delta_pct)})"
        else:
            delta_line = "▫️ 전일 대비: 기준일(최초 리포트)"

        with self.pos.lock:
            state = self.pos.state
            ep = self.pos.entry_price
        position_lines = ["▫️ 포지션: 없음"]
        if state == "POSITION" and ep > 0 and price > 0:
            unreal = (price - ep) * btc
            unreal_pct = (price / ep - 1) * 100
            position_lines = [
                f"▫️ 포지션: 보유 (평단 <b>{fmt_krw(ep)}</b>)",
                f"▫️ 평가손익: {fmt_pnl(unreal)} ({fmt_pct(unreal_pct)})",
            ]
        elif state == "SETUP":
            position_lines = ["▫️ 포지션: SETUP (매수 대기)"]

        fng = fetch_fear_greed()

        msg = (
            f"📅 {b('일일 리포트')}\n\n"
            f"▫️ 총자산: <b>{fmt_krw(total)}</b>\n"
            f"{delta_line}\n"
            f"▫️ 누적 실현손익: {fmt_pnl(self.cumulative_pnl)}\n\n"
            f"📊 {b('포지션 현황')}\n"
            + "\n".join(position_lines)
            + "\n\n"
            f"🌍 {b('시장 지표')}\n"
            f"▫️ BTC/KRW: <b>{fmt_krw(price)}</b>\n"
            f"▫️ 공포/탐욕: <b>{esc(fng)}</b>"
        )
        self.noti.send_msg(msg)
        self.previous_day_total = total

    # ── 텍스트 빌더 (명령어용) ─────────────────────────────────────────
    def build_status_text(self):
        krw = self.upbit.get_balance("KRW") or 0.0
        btc = self.upbit.get_balance(TICKER) or 0.0
        price = self.current_price or 0.0
        btc_value = btc * price
        total = krw + btc_value
        cash_ratio = (krw / total * 100) if total > 0 else 0.0
        btc_ratio = (btc_value / total * 100) if total > 0 else 0.0

        with self.pos.lock:
            state = self.pos.state
            ep = self.pos.entry_price
            bp = self.pos.breakout_price
            tp1 = self.pos.tp1_done

        paused = self.is_paused()
        state_label = f"<b>{esc(state)}</b>" + (" ⏸️" if paused else "")

        lines = [
            f"📊 {b('QuantBot 상태')}",
            "",
            f"▫️ 상태: {state_label}",
            f"▫️ 현재가: <b>{fmt_krw(price)}</b>",
            "",
            f"💰 {b('자산')}",
            f"▫️ 총자산: <b>{fmt_krw(total)}</b>",
            f"▫️ BTC: {btc:.8f} ({fmt_krw_short(btc_value)}, {fmt_pct(btc_ratio, signed=False)})",
            f"▫️ KRW: <b>{fmt_krw(krw)}</b> ({fmt_pct(cash_ratio, signed=False)})",
        ]
        if state == "SETUP":
            lines += ["", f"📈 {b('포지션')}", f"▫️ 돌파 기준가: <b>{fmt_krw(bp)}</b>"]
        elif state == "POSITION" and ep > 0 and price > 0:
            pnl_pct = (price / ep - 1) * 100
            unreal = (price - ep) * btc
            lines += [
                "",
                f"📈 {b('포지션')}",
                f"▫️ 평단가: <b>{fmt_krw(ep)}</b>",
                f"▫️ 수익률: <b>{fmt_pct(pnl_pct)}</b>",
                f"▫️ 평가손익: {fmt_pnl(unreal)}",
                f"▫️ 1차 익절: {'완료' if tp1 else '대기'}",
            ]
        lines += ["", f"💹 누적 실현손익: {fmt_pnl(self.cumulative_pnl)}"]
        return "\n".join(lines)

    def build_market_text(self):
        snap = DataProvider.get_market_snapshot()
        price = snap.get("price") or self.current_price
        chg = snap.get("change_pct_24h")
        vol = snap.get("volume_24h_krw")
        rsi = snap.get("rsi_15m")
        atr = snap.get("atr_15m")
        ema20 = snap.get("ema20_1h")
        ema50 = snap.get("ema50_1h")
        adx = snap.get("adx_1h")
        fng = snap.get("fng")

        if ema20 is not None and ema50 is not None:
            arrangement = "정배열" if ema20 > ema50 else "역배열"
            ema_line = (
                f"▫️ EMA20/50: <b>{arrangement}</b> "
                f"({fmt_krw_short(ema20)} / {fmt_krw_short(ema50)})"
            )
        else:
            ema_line = "▫️ EMA: N/A"

        if adx is not None:
            trend_label = "강함" if adx >= 28 else ("보통" if adx >= 20 else "약함")
            adx_line = f"▫️ ADX(14): <b>{adx:.1f}</b> (추세 {trend_label})"
        else:
            adx_line = "▫️ ADX(14): N/A"

        vol_line = (
            f"▫️ 24h 거래대금: {fmt_krw_short(vol)}" if vol else "▫️ 24h 거래대금: N/A"
        )
        chg_line = f"▫️ 24h 변동: <b>{fmt_pct(chg)}</b>" if chg is not None else "▫️ 24h 변동: N/A"
        rsi_line = f"▫️ RSI(14): <b>{rsi:.1f}</b>" if rsi is not None else "▫️ RSI(14): N/A"
        atr_line = f"▫️ ATR(14): <b>{fmt_krw(atr)}</b>" if atr is not None else "▫️ ATR(14): N/A"

        return (
            f"🌍 {b('시장 스캐닝')}\n\n"
            f"▫️ BTC/KRW: <b>{fmt_krw(price)}</b>\n"
            f"{chg_line}\n"
            f"{vol_line}\n\n"
            f"📊 {b('지표 (15분봉)')}\n"
            f"{rsi_line}\n"
            f"{atr_line}\n\n"
            f"📈 {b('추세 (1시간봉)')}\n"
            f"{ema_line}\n"
            f"{adx_line}\n\n"
            f"😨 공포/탐욕: <b>{esc(fng)}</b>"
        )

    def build_history_text(self):
        with self.state_lock:
            items = list(self.trade_history)[-5:]
            cum = self.cumulative_pnl
        if not items:
            return f"📜 {b('최근 거래 내역')}\n\n기록 없음"

        lines = [f"📜 {b('최근 거래 내역')}", ""]
        for i, t in enumerate(reversed(items), 1):
            side_emoji = "🔴" if t["side"] == "BUY" else "🔵"
            side_label = "매수" if t["side"] == "BUY" else "매도"
            pnl_line = f"\n   ▫️ 확정 손익: {fmt_pnl(t['pnl'])}" if t.get("pnl") is not None else ""
            lines.append(
                f"<b>{i}.</b> {esc(t['time'])} {side_emoji} <b>{side_label}</b>\n"
                f"   ▫️ <b>{fmt_krw(t['price'])}</b> × {t['volume']:.8f} BTC{pnl_line}\n"
                f"   ▫️ 근거: {esc(t['reason'])}"
            )
        lines.append("")
        lines.append(f"💹 누적 실현손익: {fmt_pnl(cum)}")
        return "\n".join(lines)

    # ── 긴급 청산 (패닉/수동) ─────────────────────────────────────────
    def emergency_liquidate(self, reason: str, pause_after: bool):
        with self.pos.lock:
            ep = self.pos.entry_price
            was_in_position = self.pos.state == "POSITION"
        price = self.current_price

        ok, sold, uuid = self.executor.safe_market_sell(TICKER, 1.0)
        if not ok:
            self.noti.send_msg(f"❌ {b('매도 실패.')} 업비트 잔고나 API 상태를 확인하세요.")
            return

        fill = self.executor.get_avg_fill_price(uuid, price)
        realized = None
        if was_in_position and ep > 0 and sold > 0:
            realized = calc_realized_pnl(fill, ep, sold)
        self.record_trade("SELL", fill, sold, reason, pnl=realized)
        price = fill or price
        self.pos.reset()
        self._save_state()

        if pause_after:
            self.set_paused(True)

        krw = self.upbit.get_balance("KRW") or 0.0
        btc = self.upbit.get_balance(TICKER) or 0.0
        btc_value = btc * price if price else 0.0
        total = krw + btc_value
        cash_ratio = (krw / total * 100) if total > 0 else 0.0

        lines = [
            f"🔵 {b('[긴급 전량매도 완료]')}",
            "",
            f"▫️ 매도가: <b>{fmt_krw(price)}</b>",
            f"▫️ 체결량: {sold:.8f} BTC ({fmt_krw_short(price*sold if price else 0)})",
            f"▫️ 근거: {esc(reason)}",
        ]
        if realized is not None:
            lines.append(f"▫️ 확정 손익: {fmt_pnl(realized)}")
        lines += [
            "",
            f"💰 현재 잔고: 현금 <b>{fmt_krw(krw)}</b> (잔여 비중 {fmt_pct(cash_ratio, signed=False)})",
            f"💹 누적 실현손익: {fmt_pnl(self.cumulative_pnl)}",
        ]
        if pause_after:
            lines += ["", "⏸️ 일시정지 ON — /resume 으로 재개"]
        self.noti.send_msg("\n".join(lines))

    # ── 알림 빌더 (매수/매도) ─────────────────────────────────────────
    def _send_buy_notice(self, fill_price, volume, atr, reason):
        krw = self.upbit.get_balance("KRW") or 0.0
        btc = self.upbit.get_balance(TICKER) or 0.0
        btc_value = btc * fill_price if fill_price else 0.0
        total = krw + btc_value
        cash_ratio = (krw / total * 100) if total > 0 else 0.0
        order_value = fill_price * volume if (fill_price and volume) else 0.0

        self.noti.send_msg(
            f"🔴 {b('[신규 매수 체결] BTC/KRW')}\n\n"
            f"▫️ 매수가: <b>{fmt_krw(fill_price)}</b>\n"
            f"▫️ 체결량: {volume:.8f} BTC ({fmt_krw_short(order_value)})\n"
            f"▫️ 매수 근거: {esc(reason)}\n"
            f"▫️ ATR(15m): {fmt_krw(atr)}\n\n"
            f"💰 현재 잔고: 현금 <b>{fmt_krw(krw)}</b> (잔여 비중 {fmt_pct(cash_ratio, signed=False)})"
        )

    def _send_sell_notice(self, header, fill_price, sold_volume, reason, realized):
        krw = self.upbit.get_balance("KRW") or 0.0
        btc = self.upbit.get_balance(TICKER) or 0.0
        btc_value = btc * fill_price if fill_price else 0.0
        total = krw + btc_value
        order_value = fill_price * sold_volume if (fill_price and sold_volume) else 0.0

        lines = [
            f"{header}",
            "",
            f"▫️ 매도가: <b>{fmt_krw(fill_price)}</b>",
            f"▫️ 체결량: {sold_volume:.8f} BTC ({fmt_krw_short(order_value)})",
            f"▫️ 매도 근거: {esc(reason)}",
        ]
        if realized is not None:
            label = "확정 수익" if realized >= 0 else "확정 손실"
            lines.append(f"▫️ {label}: {fmt_pnl(realized)}")
        lines += [
            "",
            f"💰 총자산: <b>{fmt_krw(total)}</b> (현금 {fmt_krw_short(krw)})",
            f"💹 누적 실현손익: {fmt_pnl(self.cumulative_pnl)}",
        ]
        self.noti.send_msg("\n".join(lines))

    # ── 메인 루프 ──────────────────────────────────────────────────────
    def run(self):
        self.noti.start_listening()
        self.noti.send_msg(
            f"⚙️ {b('QuantBot V4.0 기동')}\n\n"
            "▫️ 명령어 안내: /all"
        )
        logger.info("메인 루프 시작")

        while True:
            try:
                now = now_kst()
                self.check_websocket_health()
                self.check_memory()
                self.maybe_send_daily_briefing()

                if self.current_price <= 0:
                    time.sleep(0.5)
                    continue

                if now.minute % 5 == 0 and now.second < 2 and now >= self.flash_crash_until:
                    if DataProvider.is_flash_crash_active():
                        self.flash_crash_until = now + datetime.timedelta(hours=6)
                        logger.warning("서킷 브레이커 발동: 6시간 진입 차단")
                        self.noti.send_msg(
                            f"🚨 {b('[서킷 브레이커 발동]')}\n\n"
                            "▫️ 5분봉 -5% 이상 급락 감지\n"
                            "▫️ <b>6시간 신규 진입 차단</b>\n"
                            "▫️ 보유 포지션은 즉시 전량 청산 시도"
                        )
                        with self.pos.lock:
                            flash_state = self.pos.state
                        if flash_state == "POSITION":
                            self.emergency_liquidate(reason="플래시 크래시 서킷브레이커", pause_after=False)
                        elif flash_state == "SETUP":
                            self.pos.reset()
                            self._save_state()
                        else:
                            self._save_state()

                is_locked = (now < self.cooldown_until) or (now < self.flash_crash_until)
                paused = self.is_paused()

                if (now - self.last_data_fetch).total_seconds() >= 60:
                    data_1h, data_15m = DataProvider.get_market_data()
                    self.last_data_fetch = now
                    if data_1h is None:
                        continue

                    if len(data_15m) < 2:
                        continue
                    closed_15m = data_15m.iloc[-2]
                    atr_15m = closed_15m["atr"]
                    rsi_15m = closed_15m["rsi"]
                    cur_volume_15m = closed_15m["volume"]
                    vol_ma20_15m = closed_15m["vol_ma20"]
                    highest_target = closed_15m["highest"]
                    closed_close_15m = closed_15m["close"]
                    current_closed_candle_time = data_15m.index[-2]

                    with self.pos.lock:
                        current_state = self.pos.state
                        breakout_price = self.pos.breakout_price

                    if paused and current_state in ("IDLE", "SETUP"):
                        if current_state == "SETUP":
                            self.pos.reset()
                            self._save_state()
                            logger.info("SETUP 취소: 일시정지")
                            self.noti.send_msg(f"⏸️ {b('SETUP 취소')} (일시정지)")

                    elif current_state == "IDLE" and not is_locked:
                        is_uptrend = (
                            (data_1h["adx"] > ADX_MIN)
                            and (data_1h["ema20"] > data_1h["ema50"])
                            and (self.current_price > data_1h["ema20"] * 1.005)
                        )
                        # ── 추가 진입 필터 (walk-forward 검증 결과 반영) ─────────
                        # ① RSI 과매수 차단
                        rsi_ok = (rsi_15m is not None) and (rsi_15m < RSI_MAX)
                        # ② 거래량 확인 (현재봉 거래량 > VOL_MULT × 20봉 평균)
                        vol_ok = (
                            vol_ma20_15m is not None and vol_ma20_15m > 0
                            and cur_volume_15m > vol_ma20_15m * VOL_MULT
                        )
                        # ③ 시간 필터 (유동성 저조 구간 차단)
                        kst_hour = now.hour
                        time_ok = not (BLOCK_HOUR_START <= kst_hour <= BLOCK_HOUR_END)

                        if (is_uptrend
                                and (self.current_price > highest_target)
                                and rsi_ok and vol_ok and time_ok):
                            self.pos.enter_setup(highest_target)
                            self.last_closed_candle_time = current_closed_candle_time
                            self._save_state()
                            self.last_entry_reason = (
                                f"15분봉 Highest_{HIGHEST_N} 돌파 "
                                f"(ADX {data_1h['adx']:.1f}, RSI {rsi_15m:.1f})"
                            )
                            logger.info(f"SETUP 진입 (기준가 {highest_target:,.0f})")
                            self.noti.send_msg(
                                f"🟡 {b('[SETUP 진입]')}\n\n"
                                f"▫️ 돌파 기준가: <b>{fmt_krw(highest_target)}</b>\n"
                                f"▫️ ADX(1h): <b>{data_1h['adx']:.1f}</b>\n"
                                f"▫️ RSI(15m): <b>{rsi_15m:.1f}</b>\n"
                                f"▫️ 거래량: <b>{cur_volume_15m/vol_ma20_15m:.2f}× MA20</b>\n"
                                "▫️ 돌파 안착 확인 대기 중"
                            )

                    elif current_state == "SETUP" and not is_locked:
                        if data_1h["adx"] < ADX_MIN or data_1h["ema20"] < data_1h["ema50"]:
                            self.pos.reset()
                            self._save_state()
                            logger.info("SETUP 취소: 상위 추세 붕괴")
                            self.noti.send_msg(f"🚫 {b('SETUP 취소')} (상위 추세 붕괴)")
                            continue

                        if self.last_closed_candle_time != current_closed_candle_time:
                            self.last_closed_candle_time = current_closed_candle_time
                            if closed_close_15m > breakout_price:
                                if (atr_15m / self.current_price) < ATR_MIN_PCT:
                                    self.pos.reset()
                                    self._save_state()
                                    logger.info("SETUP 취소: 변동성 부족")
                                    self.noti.send_msg(f"🚫 {b('SETUP 취소')} (변동성 {ATR_MIN_PCT*100:.2f}% 미만)")
                                    continue

                                krw_bal = self.upbit.get_balance("KRW")
                                budget = RiskManager.get_position_size(
                                    krw_bal, self.current_price, atr_15m
                                )
                                if budget > 5000:
                                    uuid = self.executor.safe_market_buy(TICKER, budget)
                                    if uuid:
                                        time.sleep(1.5)
                                        actual_btc_bal = self.upbit.get_balance(TICKER)
                                        avg_buy = self.upbit.get_avg_buy_price(TICKER)
                                        if actual_btc_bal and avg_buy and (actual_btc_bal * avg_buy) > 5000:
                                            self.pos.enter_position(avg_buy, atr_15m)
                                            self._save_state()
                                            logger.info(f"POSITION 진입 (평단 {avg_buy:,.0f})")
                                            reason = self.last_entry_reason or "15분봉 돌파 안착"
                                            self.record_trade(
                                                "BUY", avg_buy, actual_btc_bal, reason, pnl=None
                                            )
                                            self._send_buy_notice(
                                                avg_buy, actual_btc_bal, atr_15m, reason
                                            )
                                        else:
                                            self.pos.reset()
                                            self._save_state()
                                            logger.warning("잔고 검증 실패로 포지션 롤백")
                                            self.noti.send_msg(
                                                f"⚠️ {b('매수 후 잔고 검증 실패')}. SETUP 해제"
                                            )
                                    else:
                                        self.pos.reset()
                                        self._save_state()
                                        logger.warning("매수 주문 실패")
                                        self.noti.send_msg(
                                            f"⚠️ {b('매수 주문 실패')}. SETUP 해제"
                                        )
                                else:
                                    self.pos.reset()
                                    self._save_state()
                                    logger.info("SETUP 취소: 예산 5000원 미만")
                            else:
                                self.pos.reset()
                                self._save_state()
                                logger.info("SETUP 취소: 돌파 안착 실패")

                with self.pos.lock:
                    in_position = self.pos.state == "POSITION" and self.pos.entry_price > 0
                    ep = self.pos.entry_price
                    atr_entry = self.pos.atr_at_entry
                    tp1_done = self.pos.tp1_done

                if in_position:
                    safe_atr = atr_entry if atr_entry > 0 else ep * 0.02
                    stop_price = ep + (TRAIL_ATR * safe_atr) if tp1_done else ep - (SL_ATR * safe_atr)
                    tp1_price = ep + (TP1_ATR * safe_atr)
                    tp2_price = ep + (TP2_ATR * safe_atr)

                    if self.current_price <= stop_price:
                        ok, sold, uuid = self.executor.safe_market_sell(TICKER, 1.0)
                        if ok:
                            fill = self.executor.get_avg_fill_price(uuid, self.current_price)
                            realized = calc_realized_pnl(fill, ep, sold) if sold > 0 else 0.0
                            if tp1_done:
                                reason = f"트레일 컷 (+{TRAIL_ATR} ATR 익절 트레일)"
                                header = f"🛡️ {b(f'[트레일 컷] +{TRAIL_ATR} ATR')}"
                            else:
                                reason = f"손절 (-{SL_ATR} ATR)"
                                header = f"📉 {b(f'[손절 체결] -{SL_ATR} ATR')}"
                                self.cooldown_until = now + datetime.timedelta(hours=3)
                            self.record_trade("SELL", fill, sold, reason, pnl=realized)
                            self._send_sell_notice(header, fill, sold, reason, realized)
                            if not tp1_done:
                                self.noti.send_msg(
                                    f"⏳ {b('3시간 쿨다운 시작')}\n\n"
                                    "▫️ 손절 이후 뇌동매매 방지"
                                )
                            self.pos.reset()
                            self._save_state()

                    elif self.current_price >= tp1_price and not tp1_done:
                        ok, sold, uuid = self.executor.safe_market_sell(TICKER, TP1_RATIO)
                        if ok:
                            fill = self.executor.get_avg_fill_price(uuid, self.current_price)
                            realized = calc_realized_pnl(fill, ep, sold) if sold > 0 else 0.0
                            reason = f"1차 익절 +{TP1_ATR} ATR ({TP1_RATIO*100:.0f}%)"
                            with self.pos.lock:
                                self.pos.tp1_done = True
                            self._save_state()
                            self.record_trade("SELL", fill, sold, reason, pnl=realized)
                            self._send_sell_notice(
                                f"🔵 {b(f'[1차 익절 체결] +{TP1_ATR} ATR ({TP1_RATIO*100:.0f}%)')}",
                                fill, sold, reason, realized,
                            )

                    elif self.current_price >= tp2_price and tp1_done:
                        ok, sold, uuid = self.executor.safe_market_sell(TICKER, 1.0)
                        if ok:
                            fill = self.executor.get_avg_fill_price(uuid, self.current_price)
                            realized = calc_realized_pnl(fill, ep, sold) if sold > 0 else 0.0
                            reason = f"2차 최종 익절 +{TP2_ATR} ATR (전량)"
                            self.record_trade("SELL", fill, sold, reason, pnl=realized)
                            self._send_sell_notice(
                                f"🔥 {b(f'[최종 익절 체결] +{TP2_ATR} ATR (전량)')}",
                                fill, sold, reason, realized,
                            )
                            self.pos.reset()
                            self._save_state()

            except Exception as e:
                now = now_kst()
                if (now - self.last_error_time).total_seconds() > 60:
                    logger.error(f"메인 루프 에러: {e}", exc_info=True)
                    self.noti.send_msg(f"🚨 {b('봇 에러')}: {esc(e)}")
                    self.last_error_time = now
            time.sleep(0.5)


if __name__ == "__main__":
    QuantStrategy().run()
