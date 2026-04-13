import os
import sys
import time
import datetime
import threading
import logging
import queue
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pyupbit
import ta
import telebot
from dotenv import load_dotenv


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

if not ACCESS_KEY or not SECRET_KEY or not TELEGRAM_TOKEN or not str(CHAT_ID or "").strip():
    logger.error("🚨 .env 확인: 키/토큰/채팅ID 누락")
    sys.exit(1)


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
            df_15m["highest_40"] = df_15m["high"].shift(1).rolling(window=40).max()

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


class RiskManager:
    @staticmethod
    def get_position_size(krw_balance, current_price, atr_value):
        if atr_value <= 0 or current_price <= 0:
            return 0.0
        risk_amount = krw_balance * 0.01
        stop_loss_pct = (1.5 * atr_value) / current_price
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
        try:
            initial_volume = self.upbit.get_balance(ticker)
            sell_volume = round(initial_volume * volume_ratio, 8)
            if sell_volume <= 0:
                return False

            order = self.upbit.sell_market_order(ticker, sell_volume)
            if order is None or (isinstance(order, dict) and "error" in order):
                logger.error(f"매도 API 거절: {order}")
                return False

            for _ in range(5):
                time.sleep(1)
                current_volume = self.upbit.get_balance(ticker)
                if volume_ratio >= 0.99 and current_volume < initial_volume * 0.1:
                    return True
                if volume_ratio == 0.3 and current_volume < initial_volume * 0.75:
                    return True
            logger.warning("체결 확인 실패: 잔고 변동 부족")
            return False
        except Exception as e:
            logger.error(f"매도 실행 에러: {e}")
            return False


class TelegramManager:
    def __init__(self, token, chat_id, strategy_instance):
        self.bot = telebot.TeleBot(token)
        self.chat_id = str(chat_id).strip()
        self.strategy = strategy_instance

        @self.bot.message_handler(commands=["status"])
        def send_status(message):
            if str(message.chat.id) != self.chat_id:
                return
            with self.strategy.pos.lock:
                state = self.strategy.pos.state
                bp = self.strategy.pos.breakout_price
                ep = self.strategy.pos.entry_price
                tp1 = self.strategy.pos.tp1_done

            price = self.strategy.current_price
            msg = f"📊 상태: {state}\n현재가: {price:,.0f}원\n"
            if state == "SETUP":
                msg += f"돌파 기준가: {bp:,.0f}원\n"
            elif state == "POSITION" and ep > 0:
                msg += f"평단가: {ep:,.0f}원\n1차 익절: {tp1}\n수익률: {(price / ep - 1) * 100:.2f}%\n"
            self.bot.reply_to(message, msg)

        @self.bot.message_handler(commands=["balance"])
        def send_balance(message):
            if str(message.chat.id) != self.chat_id:
                return
            krw = self.strategy.upbit.get_balance("KRW")
            btc = self.strategy.upbit.get_balance(TICKER)
            price = self.strategy.current_price
            total_est = krw + (btc * price) if price else krw
            msg = f"💰 KRW: {krw:,.0f}원\nBTC: {btc:.8f}\n총자산(추정): {total_est:,.0f}원"
            self.bot.reply_to(message, msg)

    def send_msg(self, text):
        try:
            self.bot.send_message(self.chat_id, text)
            logger.info(f"텔레그램: {text.replace(chr(10), ' ')}")
        except Exception as e:
            logger.error(f"텔레그램 전송 실패: {e}")

    def start_listening(self):
        threading.Thread(target=self.bot.infinity_polling, daemon=True).start()


class QuantStrategy:
    def __init__(self):
        self.upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)
        self.executor = Execution(self.upbit)
        self.pos = Position()
        self.noti = TelegramManager(TELEGRAM_TOKEN, CHAT_ID, self)

        self.current_price = 0.0
        self.last_ws_update = datetime.datetime.now()
        self.cooldown_until = datetime.datetime.now()
        self.flash_crash_until = datetime.datetime.now()
        self.last_data_fetch = datetime.datetime.now() - datetime.timedelta(minutes=1)
        self.last_closed_candle_time = None
        self.last_error_time = datetime.datetime.now() - datetime.timedelta(hours=1)
        self.wm = None

        self.init_websocket()
        self.sync_account_on_startup()

    def init_websocket(self):
        try:
            if self.wm is not None:
                self.wm.terminate()
                time.sleep(1)
            self.wm = pyupbit.WebSocketManager("ticker", [TICKER])
            # pyupbit는 get() 호출 시 프로세스를 시작하므로,
            # 지연 시작으로 큐 병목이 생기지 않게 명시적으로 기동합니다.
            self.wm.alive = True
            self.wm.start()
            self.last_ws_update = datetime.datetime.now()
            logger.info("웹소켓 초기화 성공")
        except Exception as e:
            logger.error(f"웹소켓 초기화 실패: {e}")

    def sync_account_on_startup(self):
        try:
            btc_bal = self.upbit.get_balance(TICKER)
            avg_buy_price = self.upbit.get_avg_buy_price(TICKER)
            if btc_bal and avg_buy_price and (btc_bal * avg_buy_price) > 5000:
                self.pos.sync_to_existing_position(avg_buy_price)
                logger.info(f"기존 보유 동기화 완료 (평단 {avg_buy_price:,.0f})")
        except Exception as e:
            logger.warning(f"초기 계좌 동기화 실패: {e}")

    def check_websocket_health(self):
        now = datetime.datetime.now()
        try:
            drained = 0
            latest_price = None

            if self.wm is not None:
                q = getattr(self.wm, "_WebSocketManager__q", None)
                if q is not None:
                    # 큐를 한 번에 비워 최신 틱만 반영해 stale price를 방지합니다.
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
                self.noti.send_msg("🔄 웹소켓 응답 없음. 재연결 시도")
                self.init_websocket()
                fallback = pyupbit.get_current_price(TICKER)
                if fallback:
                    self.current_price = fallback
                    self.last_ws_update = now
        except Exception as e:
            logger.warning(f"웹소켓 헬스체크 에러: {e}")

    def run(self):
        self.noti.start_listening()
        self.noti.send_msg("🚀 V4.5 봇 시작\n명령어: /status /balance")
        logger.info("메인 루프 시작")

        while True:
            try:
                now = datetime.datetime.now()
                self.check_websocket_health()
                if self.current_price <= 0:
                    time.sleep(0.5)
                    continue

                if now.minute % 5 == 0 and now.second < 2:
                    if DataProvider.is_flash_crash_active():
                        self.flash_crash_until = now + datetime.timedelta(hours=6)
                        self.pos.reset()
                        logger.warning("서킷 브레이커 발동: 6시간 진입 차단")
                        self.noti.send_msg("🚨 서킷 브레이커 발동. 6시간 신규 진입 금지")

                is_locked = (now < self.cooldown_until) or (now < self.flash_crash_until)

                if (now - self.last_data_fetch).total_seconds() >= 60:
                    data_1h, data_15m = DataProvider.get_market_data()
                    self.last_data_fetch = now
                    if data_1h is None:
                        continue

                    atr_15m = data_15m["atr"].iloc[-1]
                    highest_target = data_15m["highest_40"].iloc[-1]
                    prev_close_15m = data_15m["close"].iloc[-2]
                    current_closed_candle_time = data_15m.index[-2]

                    with self.pos.lock:
                        current_state = self.pos.state
                        breakout_price = self.pos.breakout_price

                    if current_state == "IDLE" and not is_locked:
                        is_uptrend = (
                            (data_1h["adx"] > 28)
                            and (data_1h["ema20"] > data_1h["ema50"])
                            and (self.current_price > data_1h["ema20"] * 1.005)
                        )
                        if is_uptrend and (self.current_price > highest_target):
                            self.pos.enter_setup(highest_target)
                            logger.info(f"SETUP 진입 (기준가 {highest_target:,.0f})")
                            self.noti.send_msg(f"🟡 SETUP 진입 (기준가 {highest_target:,.0f})")

                    elif current_state == "SETUP" and not is_locked:
                        if data_1h["adx"] < 28 or data_1h["ema20"] < data_1h["ema50"]:
                            self.pos.reset()
                            logger.info("SETUP 취소: 상위 추세 붕괴")
                            self.noti.send_msg("🚫 SETUP 취소 (상위 추세 붕괴)")
                            continue

                        if self.last_closed_candle_time != current_closed_candle_time:
                            self.last_closed_candle_time = current_closed_candle_time
                            if prev_close_15m > breakout_price:
                                if (atr_15m / self.current_price) < 0.0015:
                                    self.pos.reset()
                                    logger.info("SETUP 취소: 변동성 부족")
                                    self.noti.send_msg("🚫 SETUP 취소 (변동성 0.15% 미만)")
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
                                        if actual_btc_bal > 0 and avg_buy > 0 and (actual_btc_bal * avg_buy) > 5000:
                                            self.pos.enter_position(avg_buy, atr_15m)
                                            logger.info(f"POSITION 진입 (평단 {avg_buy:,.0f})")
                                            self.noti.send_msg(f"🔴 POSITION 진입\n평단가: {avg_buy:,.0f}원")
                                        else:
                                            self.pos.reset()
                                            logger.warning("잔고 검증 실패로 포지션 롤백")
                                            self.noti.send_msg("⚠️ 매수 후 잔고 검증 실패. SETUP 해제")
                                    else:
                                        self.pos.reset()
                                        logger.warning("매수 주문 실패")
                                        self.noti.send_msg("⚠️ 매수 주문 실패. SETUP 해제")
                                else:
                                    self.pos.reset()
                                    logger.info("SETUP 취소: 예산 5000원 미만")
                            else:
                                self.pos.reset()
                                logger.info("SETUP 취소: 돌파 안착 실패")

                with self.pos.lock:
                    in_position = self.pos.state == "POSITION" and self.pos.entry_price > 0
                    ep = self.pos.entry_price
                    atr_entry = self.pos.atr_at_entry
                    tp1_done = self.pos.tp1_done

                if in_position:
                    safe_atr = atr_entry if atr_entry > 0 else ep * 0.02
                    stop_price = ep + (0.5 * safe_atr) if tp1_done else ep - (1.5 * safe_atr)
                    tp1_price = ep + (2.0 * safe_atr)
                    tp2_price = ep + (8.0 * safe_atr)

                    if self.current_price <= stop_price:
                        if self.executor.safe_market_sell(TICKER, 1.0):
                            if tp1_done:
                                logger.info("약익절 컷 완료")
                                self.noti.send_msg("🛡️ 약익절 컷 완료 (+0.5 ATR)")
                            else:
                                self.cooldown_until = now + datetime.timedelta(hours=3)
                                logger.info("손절 완료, 3시간 쿨다운")
                                self.noti.send_msg("🩸 손절 완료 → 3시간 쿨다운")
                            self.pos.reset()

                    elif self.current_price >= tp1_price and not tp1_done:
                        if self.executor.safe_market_sell(TICKER, 0.3):
                            with self.pos.lock:
                                self.pos.tp1_done = True
                            logger.info("1차 익절 30% 완료")
                            self.noti.send_msg("🔵 1차 익절 30% 완료 → 스탑 +0.5 ATR")

                    elif self.current_price >= tp2_price and tp1_done:
                        if self.executor.safe_market_sell(TICKER, 1.0):
                            self.pos.reset()
                            logger.info("2차 최종 익절 완료")
                            self.noti.send_msg("🔥 2차 최종 익절 완료")

            except Exception as e:
                now = datetime.datetime.now()
                if (now - self.last_error_time).total_seconds() > 60:
                    logger.error(f"메인 루프 에러: {e}", exc_info=True)
                    self.noti.send_msg(f"🚨 봇 에러: {e}")
                    self.last_error_time = now
            time.sleep(0.5)


if __name__ == "__main__":
    QuantStrategy().run()