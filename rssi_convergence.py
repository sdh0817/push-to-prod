"""목표 RSSI 수렴 로직 (Step 04).

ATT6000 감쇠 값을 자동 조정하여 실측 RSSI가 목표값 ±tolerance 범위에
들어오도록 수렴시킨다. att6000.ATT6000 과 rssi_reader 를 사용한다.

제어 모델:
    RSSI ≈ RSSI0 - ATT  (감쇠 1 dB 증가 → RSSI 약 1 dBm 감소)
    보정량 = (실측RSSI - 목표RSSI) 를 ATT에 더하는 1:1 비례 제어로 수렴.

  · 실측 RSSI > 목표 + tolerance  → 신호가 강함 → ATT 증가
  · 실측 RSSI < 목표 - tolerance  → 신호가 약함 → ATT 감소
"""

from __future__ import annotations

import time

from att6000 import ATT6000, MAX_DB, MIN_DB, STEP_DB
from rssi_reader import wait_for_rssi_stable


def _snap(db: float) -> float:
    """0.25 dB 단위 반올림 + [MIN_DB, MAX_DB] 클램프."""
    db = round(db / STEP_DB) * STEP_DB
    return round(min(max(db, MIN_DB), MAX_DB), 2)


def converge_to_rssi(
    att: ATT6000,
    interface: str,
    target_rssi: float,
    tolerance: float = 2.0,
    timeout: float = 30.0,
    step: float = 1.0,
    settle: float = 2.5,
) -> dict:
    """실측 RSSI를 target_rssi ±tolerance 로 수렴.

    settle: 감쇠 변경 후 WiFi 어댑터가 새 RSSI를 보고하기까지의 안정화 대기(초).
            (어댑터 RSSI 갱신이 수 초 지연되므로 측정 전 필수)

    반환 dict — 항상 success/att_db/measured_rssi/elapsed 포함:
        성공: {"success": True,  "att_db": 12.5, "measured_rssi": -64.2, "elapsed": 12.3}
        실패: {..., "success": False, "reason": "timeout"|"max_att"|"min_att"|"link_lost"}
    """
    start = time.monotonic()
    cur_att = att.get_attenuation()
    last_rssi: float | None = None

    def result(success, reason=None):
        out = {"success": success, "att_db": cur_att,
               "measured_rssi": last_rssi,
               "elapsed": round(time.monotonic() - start, 1)}
        if reason:
            out["reason"] = reason
        return out

    while True:
        if time.monotonic() - start > timeout:
            return result(False, "timeout")

        time.sleep(settle)  # 감쇠 변경이 RSSI에 반영될 때까지 대기
        measured = wait_for_rssi_stable(interface, samples=3, interval=1.0)
        last_rssi = measured

        # 링크 끊김 → 신호 늘리기(ATT 감소) 후 재시도
        if measured is None:
            if cur_att <= MIN_DB:
                return result(False, "link_lost")
            cur_att = _snap(cur_att - max(step, STEP_DB))
            att.set_attenuation(cur_att)
            print(f"  [수렴 중] 링크 끊김 → ATT 감소 {cur_att:.2f}dB 재시도")
            continue

        print(f"  [수렴 중] ATT={cur_att:.1f}dB → RSSI={measured:.1f}dBm "
              f"(목표: {target_rssi:.1f}dBm)")

        # 수렴 완료
        if abs(measured - target_rssi) <= tolerance:
            return result(True)

        # 비례 보정 (+면 신호 강함 → ATT↑ / -면 약함 → ATT↓)
        correction = measured - target_rssi
        new_att = _snap(cur_att + correction)

        # 미세 오차로 정체될 때 최소 한 스텝 강제 이동
        if new_att == cur_att:
            new_att = _snap(cur_att + (STEP_DB if correction > 0 else -STEP_DB))

        # 경계(최소/최대)에 막혀 더 못 움직이면 실패 종료
        if new_att == cur_att:
            return result(False, "max_att" if correction > 0 else "min_att")

        cur_att = new_att
        att.set_attenuation(cur_att)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("사용법: python rssi_convergence.py <COM포트> [인터페이스] [목표RSSI]")
        sys.exit(1)
    port = sys.argv[1]
    iface = sys.argv[2] if len(sys.argv) > 2 else "Wi-Fi 2"
    target = float(sys.argv[3]) if len(sys.argv) > 3 else -65.0
    with ATT6000(port) as att:
        att.set_attenuation(0.0)
        print("결과:", converge_to_rssi(att, iface, target))
        att.set_attenuation(0.0)
