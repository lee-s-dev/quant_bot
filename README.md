🚀 QuantBot V4.0: 스마트 비트코인 돌파 매매 봇
본 프로젝트는 업비트(Upbit) 거래소를 기반으로 한 비트코인(BTC) 자동매매 시스템입니다. 상위 추세 확인과 변동성 기반의 돌파 전략을 결합하여 안정적인 수익을 추구하며, 텔레그램을 통해 실시간 제어 및 모니터링이 가능합니다.

🛠 주요 기술 및 라이브러리
Language: Python 3.10+

Trading: pyupbit (업비트 API 연동)

Analysis: ta (Technical Analysis Library - ADX, EMA, ATR)

Ops/Automation: PM2 (무중단 프로세스 관리), Claude Code (서버 디버깅)

Monitoring: python-telegram-bot (실시간 알림 및 명령)

📈 핵심 매매 전략 (V4.0)
1. 추세 및 필터링 (Top-Down 분석)
1시간 봉: ADX(28 이상)와 EMA(20/50) 정배열을 통해 강한 상승 추세에서만 진입을 준비합니다. * 서킷 브레이커: 5분 내 -5% 이상의 급락(Flash Crash) 감지 시 6시간 동안 신규 진입을 전면 차단합니다.

2. 진입 및 청산 로직 (Volatility Breakout)
SETUP 진입: 15분 봉 기준 최근 40개 캔들의 최고점 돌파 시 셋업에 진입합니다.

리스크 관리: 총 자산의 1% 리스크(ATR 기반) 내에서 포지션 규모를 자동으로 산출합니다.

분할 익절: * 1차 익절: +2.0 ATR 도달 시 물량의 30% 매도 및 본절가 위(+0.5 ATR)로 스탑로스 상향 조절.     * 2차 익절: +8.0 ATR 도달 시 전량 매도.

손절: -1.5 ATR 도달 시 즉시 전량 매도하여 리스크를 제한합니다.

📱 텔레그램 명령어 리스트
봇이 실행되는 동안 다음 명령어를 통해 실시간으로 상태를 확인하고 제어할 수 있습니다.

/status: 현재 포지션 상태, 평단가, 실시간 수익률 조회

/balance: KRW 잔고 및 BTC 보유 현황, 총 추정 자산 확인

/sell_all: [긴급] 현재 보유 중인 BTC 전량 즉시 매도 및 포지션 초기화

/stop: 봇 프로세스 안전 종료

/all: 모든 사용 가능한 명령어 안내

⚙️ 설치 및 설정 방법
1. 환경 변수 설정 (.env)
프로젝트 최상위 폴더에 .env 파일을 생성하고 아래 키를 입력합니다. (보안을 위해 .gitignore에 포함됨)

코드 스니펫
UPBIT_ACCESS_KEY='내_액세스_키'
SECRET_KEY='내_시크릿_키'
TELEGRAM_TOKEN='봇_토큰'
TELEGRAM_CHAT_ID='내_채팅_ID'
2. 의존성 설치
Bash
pip install pyupbit ta python-dotenv pyTelegramBotAPI requests
3. 서버 실행 (PM2 권장)
Bash
pm2 start quant_bot_4.0.py --name "btc-bot" --interpreter python3
📂 파일 구조
quant_bot_4.0.py: 메인 전략 및 실행 소스 코드

quant_bot.log: 실행 로그 기록 (5MB 단위 순환 저장)

.gitignore: 보안 파일(.env) 및 캐시 파일 제외 설정

README.md: 프로젝트 명세서

⚠️ 면책 조항 (Disclaimer)
본 봇은 작성자의 개인적인 투자 전략을 자동화한 것이며, 투자 결과에 대한 책임은 전적으로 사용자에게 있습니다. 암호화폐 시장의 변동성은 매우 크므로 충분한 테스트 후 사용하시기 바랍니다.