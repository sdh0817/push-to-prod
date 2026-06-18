# WiFi OTA RvR 자동화 (Windows)

OTA(실제 무선) 환경에서 **WTDZ ATT6000** 가변 감쇠기를 USB 시리얼로 자동 제어하며,
**RSSI 포인트별 WiFi 처리량(Rate vs Range)** 을 자동 측정하는 프로그램.

테스트 PC 자신이 DUT(측정 대상)이다. 유선 랜으로 공유기(=iperf3 서버)에 붙고,
USB 무선 어댑터로 OTA 경로를 거쳐 ATT6000을 통과해 iperf3 클라이언트로 송수신한다.

```
[PC 이더넷] ── Ethernet ── [공유기/AP] ── OTA ── [ATT6000] ── [PC Wi-Fi 2]
     │  iperf3 server                                    iperf3 client │
     └──────────────── USB Serial (ATT6000 감쇠 제어) ──────────────────┘
```

> 스펙 문서는 Linux(`iw`, `/dev/ttyUSB0`, eth0/wlan0) 기준으로 작성됐으나,
> 이 구현은 **Windows**(`netsh wlan`, `COMx`, '이더넷'/'Wi-Fi 2')에 맞춰 동작한다.

---

## 1. 구성 파일

| 파일 | 역할 | 스펙 |
|---|---|---|
| `att6000.py` | ATT6000 USB 시리얼 드라이버 (`wv` 명령, 0.25 dB 스텝) | Step 01 |
| `rssi_reader.py` | Wi-Fi RSSI 읽기 (`netsh wlan` → dBm) | Step 02 |
| `iperf3_runner.py` | iperf3 서버/클라이언트 실행·파싱 (DL/UL/Both) | Step 03 |
| `rssi_convergence.py` | 목표 RSSI ±2 dBm 수렴 (비례 제어) | Step 04 |
| `wifi_ota_rvr.py` | **메인** — 전체 통합, CSV/그래프 산출 | Step 05 |
| `plot_results.py` | CSV → RSSI vs 처리량 그래프(PNG) | — |
| `run.bat` | Windows 실행 런처 | — |

---

## 2. 사전 준비

### 의존성 (측정 PC)
```powershell
pip install pyserial matplotlib
```
- **iperf3** 필요: `winget install iperf3` (PATH 또는 winget 경로 자동 탐지)
- **위치 서비스 ON**: Windows 설정 > 개인정보 보호 > 위치 — *netsh wlan RSSI 조회에 필수*

### 하드웨어
- ATT6000이 USB로 연결되어 COM 포트로 인식 (예: `COM4`)
- `Wi-Fi 2`(USB 무선 어댑터)가 측정 대상 AP에 연결됨
- `이더넷`이 공유기에 유선 연결됨

### iperf3 서버
- **이 PC 자동 기동**(기본): 이더넷 IP에 바인딩하여 자동 실행
- **원격(노트북1)**: 노트북1에서 직접 실행
  ```bash
  iperf3 -s -B <노트북1_IP>
  ```
  (5201 포트 방화벽 허용 필요)

---

## 3. 실행

```powershell
python wifi_ota_rvr.py
```
또는 `run.bat` 더블클릭.

### 초기 입력 (Enter 시 대괄호 기본값)
```
ATT6000 포트 (COM) [COM4]     : COM4
WiFi 인터페이스명 [Wi-Fi 2]   : Wi-Fi 2
이더넷 인터페이스명 [이더넷]  : 이더넷
Band [2.4 / 5] [5]            : 5
Channel [36]                 : 36
방향 [DL / UL / Both] [Both]  : Both
목표 RSSI 목록(쉼표) [-55,-65,-75,-85]
iperf3 측정 시간(초) [10]     : 10
iperf3 서버를 이 PC에서 자동 기동? [y/n] [y]
```

### 동작
각 RSSI 포인트마다:
1. ATT6000 감쇠를 조정하며 실측 RSSI를 목표 ±2 dBm로 **자동 수렴**
2. iperf3 DL/UL **측정**
3. **CSV 기록** + 콘솔 요약 출력
4. 끝나면 전체 요약 테이블 + **그래프(PNG)** 자동 저장
5. 종료 시(또는 Ctrl+C) **ATT6000 0 dB 복원 + iperf3 서버 종료**

---

## 4. 산출물

`results/` 폴더에 자동 생성:

| 파일 | 내용 |
|---|---|
| `wifi_ota_rvr_<YYYYMMDD_HHMMSS>.csv` | 측정 결과 (UTF-8 BOM, Excel 한글 호환) |
| `wifi_ota_rvr_<...>.png` | RSSI vs 처리량 RvR 곡선 |

CSV 컬럼:
```
Timestamp,Band_GHz,Channel,ATT_dB,RSSI_dBm,Throughput_DL_Mbps,Throughput_UL_Mbps,Status
```
`Status`: `OK` / `WARN`(수렴 실패 후 강행) / `SKIP` / `FAIL` / `NO_RESULT`

---

## 5. 모듈 단독 테스트

```powershell
python att6000.py COM4            # 포트 탐지 + wv 명령 포맷 검증
python rssi_reader.py "Wi-Fi 2"   # 현재 RSSI / 3회 평균
python iperf3_runner.py           # 이더넷/Wi-Fi IP 감지
python rssi_convergence.py COM4 "Wi-Fi 2" -65   # 단일 포인트 수렴
python plot_results.py            # results/ 최신 CSV로 그래프
```

---

## 6. 트러블슈팅

| 증상 | 확인 |
|---|---|
| `ATT6000 포트 열기 실패` | COM 포트명, USB-시리얼 드라이버, 다른 프로그램 점유 여부 |
| RSSI가 항상 `None` | Wi-Fi 2 연결 상태, **위치 서비스 ON**, 인터페이스명 정확도 |
| 모든 처리량 0 / FAIL | iperf3 서버 기동, 서버/클라이언트 IP·포트·방화벽 |
| 수렴이 안 됨 | 목표 RSSI가 현재 신호로 도달 가능한 범위인지(ATT 최대 31.75 dB) |
| RSSI가 근사값 | netsh는 신호 품질(%)만 제공 → `dBm ≈ (%/2)−100`. 정밀 dBm은 Linux(`iw`) 권장 |

> **참고**: ATT6000 단일 유닛 최대 감쇠는 31.75 dB(127스텝×0.25). 더 깊은 감쇠가
> 필요하면 유닛 캐스케이드 후 `ATT6000(port, max_db=...)` 로 상한을 조정한다.
