"""WiFi OTA RvR 자동화 — GUI (tkinter).

CLI 메인(wifi_ota_rvr.py)과 동일한 검증된 엔진 모듈을 재사용하여, 입력/측정/
그래프/결과를 하나의 창에서 다룬다.

  · 입력 폼  : COM 포트 / 인터페이스 / Band / Channel / 방향 / 목표 RSSI / 측정시간 / 서버
  · 사전 점검: ATT6000 연결, Wi-Fi RSSI, iperf3 서버 도달 확인
  · 측정     : 백그라운드 스레드 (GUI 멈춤 없음), 실시간 로그
  · 그래프   : 포인트마다 RSSI vs 처리량 곡선 갱신
  · 결과     : 테이블 + CSV/PNG 자동 저장

실행: python wifi_ota_gui.py   (또는 run_gui.bat)
"""

from __future__ import annotations

import os
import queue
import socket
import sys
import threading
from datetime import datetime

import tkinter as tk
from tkinter import messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
for _f in ("Malgun Gothic", "맑은 고딕", "Gulim"):
    try:
        matplotlib.rcParams["font.family"] = _f
        break
    except Exception:
        continue
matplotlib.rcParams["axes.unicode_minus"] = False
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# ----- 검증된 엔진 모듈 재사용 -----
from att6000 import ATT6000, find_att6000_port, list_serial_ports
from iperf3_runner import (get_interface_ip, run_iperf3_client,
                           start_iperf3_server, stop_iperf3_server)
from rssi_convergence import converge_to_rssi
from rssi_reader import get_rssi
from wifi_ota_rvr import RESULTS_DIR, append_csv, ensure_csv


class QueueWriter:
    """print() 출력을 GUI 큐로 흘려보내는 stdout 대체 (수렴 로그 표시용)."""

    def __init__(self, q):
        self.q = q
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.q.put(("log", line))

    def flush(self):
        if self._buf:
            self.q.put(("log", self._buf))
            self._buf = ""


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("WiFi OTA RvR 자동화")
        root.geometry("1180x720")

        self.q: "queue.Queue" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.results: list[dict] = []
        self.csv_path: str | None = None

        self._build_inputs()
        self._build_main()
        self._build_statusbar()

        self.root.after(100, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- UI 구성 ----------------
    def _build_inputs(self):
        f = ttk.LabelFrame(self.root, text="설정", padding=8)
        f.pack(fill="x", padx=8, pady=(8, 4))
        self.var = {}
        auto_port = find_att6000_port() or "COM4"

        def row(parent, label, key, default, width=14, col=0):
            ttk.Label(parent, text=label).grid(row=0, column=col * 2, sticky="e",
                                               padx=(6, 2), pady=3)
            v = tk.StringVar(value=default)
            ttk.Entry(parent, textvariable=v, width=width).grid(
                row=0, column=col * 2 + 1, sticky="w", padx=(0, 8))
            self.var[key] = v

        g1 = ttk.Frame(f); g1.pack(fill="x")
        row(g1, "ATT 포트", "port", auto_port, 10, 0)
        row(g1, "WiFi 인터페이스", "wlan", "Wi-Fi 2", 14, 1)
        row(g1, "이더넷 인터페이스", "eth", "이더넷", 12, 2)

        g2 = ttk.Frame(f); g2.pack(fill="x", pady=(4, 0))
        row(g2, "Band(GHz)", "band", "5", 6, 0)
        row(g2, "Channel", "channel", "36", 6, 1)
        ttk.Label(g2, text="방향").grid(row=0, column=4, sticky="e", padx=(6, 2))
        self.var["direction"] = tk.StringVar(value="Both")
        ttk.Combobox(g2, textvariable=self.var["direction"], values=["DL", "UL", "Both"],
                     width=8, state="readonly").grid(row=0, column=5, sticky="w", padx=(0, 8))

        g3 = ttk.Frame(f); g3.pack(fill="x", pady=(4, 0))
        row(g3, "목표 RSSI(쉼표)", "targets", "-55,-65,-75,-85", 28, 0)
        row(g3, "측정시간(초)", "duration", "10", 6, 1)

        g4 = ttk.Frame(f); g4.pack(fill="x", pady=(4, 0))
        ttk.Label(g4, text="iperf3 서버").grid(row=0, column=0, sticky="e", padx=(6, 2))
        self.var["server_mode"] = tk.StringVar(value="local")
        ttk.Radiobutton(g4, text="이 PC 자동기동", variable=self.var["server_mode"],
                        value="local").grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(g4, text="원격(노트북1)", variable=self.var["server_mode"],
                        value="remote").grid(row=0, column=2, sticky="w", padx=(0, 8))
        row(g4, "서버 IP", "server_ip", "192.168.50.5", 16, 2)

        b = ttk.Frame(f); b.pack(fill="x", pady=(8, 0))
        self.btn_check = ttk.Button(b, text="사전 점검", command=self._on_precheck)
        self.btn_check.pack(side="left", padx=4)
        self.btn_start = ttk.Button(b, text="▶ 측정 시작", command=self._on_start)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(b, text="■ 중지", command=self._on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        ttk.Button(b, text="결과 폴더 열기", command=self._open_results).pack(side="left", padx=4)

    def _build_main(self):
        pane = ttk.Panedwindow(self.root, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.LabelFrame(pane, text="진행 로그", padding=4)
        self.log_txt = tk.Text(left, width=48, wrap="word", font=("Consolas", 9))
        sb = ttk.Scrollbar(left, command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb.set)
        self.log_txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        pane.add(left, weight=1)

        right = ttk.Frame(pane)
        gframe = ttk.LabelFrame(right, text="RvR 그래프", padding=4)
        gframe.pack(fill="both", expand=True)
        self.fig = Figure(figsize=(5.5, 3.6), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self._init_axes()
        self.canvas = FigureCanvasTkAgg(self.fig, master=gframe)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        tframe = ttk.LabelFrame(right, text="결과", padding=4)
        tframe.pack(fill="x")
        cols = ("target", "att", "rssi", "dl", "ul", "status")
        heads = ("목표RSSI", "ATT(dB)", "실측RSSI", "DL(Mbps)", "UL(Mbps)", "상태")
        self.tree = ttk.Treeview(tframe, columns=cols, show="headings", height=6)
        for c, h in zip(cols, heads):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=90, anchor="center")
        self.tree.pack(fill="x")
        pane.add(right, weight=2)

    def _build_statusbar(self):
        self.status = tk.StringVar(value="대기 중")
        ttk.Label(self.root, textvariable=self.status, relief="sunken",
                  anchor="w", padding=4).pack(fill="x", side="bottom")

    def _init_axes(self):
        self.ax.clear()
        self.ax.set_xlabel("RSSI (dBm) — 오른쪽일수록 약한 신호")
        self.ax.set_ylabel("처리량 (Mbps)")
        self.ax.set_title("WiFi OTA RvR — RSSI vs 처리량")
        self.ax.grid(True, linestyle=":", alpha=0.6)

    # ---------------- 로그/상태 ----------------
    def log(self, msg):
        self.q.put(("log", msg))

    def _append_log(self, line):
        self.log_txt.insert("end", line + "\n")
        self.log_txt.see("end")

    # ---------------- 입력 파싱 ----------------
    def _read_params(self):
        v = self.var
        try:
            targets = [float(t) for t in v["targets"].get().replace(" ", "").split(",") if t]
        except ValueError:
            raise ValueError("목표 RSSI 형식 오류 (예: -50,-60,-70)")
        if not targets:
            raise ValueError("목표 RSSI가 비어 있습니다.")
        try:
            duration = int(v["duration"].get())
        except ValueError:
            raise ValueError("측정시간은 정수(초)여야 합니다.")
        return {"port": v["port"].get().strip(), "wlan": v["wlan"].get().strip(),
                "eth": v["eth"].get().strip(), "band": v["band"].get().strip(),
                "channel": v["channel"].get().strip(), "direction": v["direction"].get(),
                "targets": targets, "duration": duration,
                "local_server": v["server_mode"].get() == "local",
                "server_ip": v["server_ip"].get().strip()}

    # ---------------- 사전 점검 ----------------
    def _on_precheck(self):
        try:
            p = self._read_params()
        except ValueError as e:
            messagebox.showerror("입력 오류", str(e)); return
        self.btn_check.config(state="disabled")
        threading.Thread(target=self._precheck_worker, args=(p,), daemon=True).start()

    def _precheck_worker(self, p):
        self.log("===== 사전 점검 =====")
        self.log(f"인식된 COM 포트: {list_serial_ports() or '없음'}")
        try:
            with ATT6000(p["port"]) as att:
                model = att.get_model()
                att.set_attenuation(0.0)
            self.log(f"[OK] ATT6000({p['port']}) 연결 — 모델: {model or '무응답'}")
        except Exception as e:
            self.log(f"[실패] ATT6000({p['port']}): {e}")
        rssi = get_rssi(p["wlan"])
        self.log(f"[OK] {p['wlan']} RSSI = {rssi} dBm" if rssi is not None
                 else f"[실패] {p['wlan']} RSSI 읽기 실패 (연결/위치서비스 확인)")
        wip = get_interface_ip(p["wlan"])
        self.log(f"{p['wlan']} IP: {wip or '없음'}")
        ip = get_interface_ip(p["eth"]) if p["local_server"] else p["server_ip"]
        if not ip:
            ip = p["server_ip"]
        if ip:
            ok = self._port_open(ip, 5201)
            self.log(f"[{'OK' if ok else '실패'}] iperf3 서버 {ip}:5201 "
                     + ("도달 가능" if ok else "도달 불가 (노트북1 iperf3 -s / 방화벽 확인)"))
        self.log("===== 점검 완료 =====")
        self.q.put(("precheck_done", None))

    @staticmethod
    def _port_open(ip, port=5201, timeout=3):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            return False

    # ---------------- 측정 ----------------
    def _on_start(self):
        try:
            p = self._read_params()
        except ValueError as e:
            messagebox.showerror("입력 오류", str(e)); return
        self.results.clear()
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._init_axes(); self.canvas.draw()
        self.stop_event.clear()
        self.btn_start.config(state="disabled")
        self.btn_check.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.status.set("측정 중...")
        self.worker = threading.Thread(target=self._measure_worker, args=(p,), daemon=True)
        self.worker.start()

    def _on_stop(self):
        self.stop_event.set()
        self.status.set("중지 요청됨 — 현재 포인트 종료 후 정리합니다...")

    def _measure_worker(self, p):
        old_stdout = sys.stdout
        sys.stdout = QueueWriter(self.q)  # converge_to_rssi 등의 print를 로그로
        server = att = None
        try:
            wlan_ip = get_interface_ip(p["wlan"])
            if not wlan_ip:
                self.log(f"[오류] {p['wlan']} IP 없음 — WiFi 연결 확인"); return

            server_ip = p["server_ip"]
            if p["local_server"]:
                server_ip = get_interface_ip(p["eth"]) or p["server_ip"]
                self.log(f"iperf3 서버 기동: -s -B {server_ip}")
                server = start_iperf3_server(server_ip)
            else:
                self.log(f"원격 iperf3 서버 사용: {server_ip} (노트북1에서 iperf3 -s 실행 중)")
            if not server_ip:
                self.log("[오류] 서버 IP가 필요합니다."); return

            att = ATT6000(p["port"]); att.open(); att.set_attenuation(0.0)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.csv_path = os.path.join(RESULTS_DIR, f"wifi_ota_rvr_{ts}.csv")
            ensure_csv(self.csv_path)

            for target in p["targets"]:
                if self.stop_event.is_set():
                    self.log("[중지] 사용자 요청으로 측정 중단"); break
                self.q.put(("status", f"측정 중... 목표 {target:.0f} dBm"))
                self.log(f"\n===== RSSI 포인트: {target:.0f} dBm =====")
                conv = converge_to_rssi(att, p["wlan"], target)
                if conv["success"]:
                    self.log(f"[수렴 완료] ATT={conv['att_db']:.2f}dB / "
                             f"RSSI={conv['measured_rssi']:.1f}dBm ({conv['elapsed']}초)")
                    status = "OK"
                else:
                    self.log(f"[수렴 실패:{conv.get('reason')}] ATT={conv['att_db']:.2f}dB "
                             f"/ RSSI={conv['measured_rssi']} — 현재값으로 강행 측정")
                    status = "WARN"

                res = run_iperf3_client(server_ip, wlan_ip, p["direction"],
                                        duration=p["duration"])
                dl, ul = res.get("DL_Mbps"), res.get("UL_Mbps")
                if res.get("status") != "OK" and status == "OK":
                    status = res["status"]
                if dl is not None:
                    self.log(f"[iperf3 DL] {dl} Mbps")
                if ul is not None:
                    self.log(f"[iperf3 UL] {ul} Mbps")

                append_csv(self.csv_path, [
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), p["band"], p["channel"],
                    f"{conv['att_db']:.2f}", conv["measured_rssi"],
                    dl if dl is not None else 0, ul if ul is not None else 0, status])
                self.log("[CSV 기록 완료]")
                self.q.put(("result", {"target": target, "att": conv["att_db"],
                                       "rssi": conv["measured_rssi"], "dl": dl or 0,
                                       "ul": ul or 0, "status": status}))

            self.q.put(("done", self.csv_path))
        except Exception as e:
            self.log(f"[오류] {e}")
            self.q.put(("error", str(e)))
        finally:
            if att is not None:
                try:
                    att.set_attenuation(0.0); self.log("ATT6000 → 0 dB 복원")
                except Exception:
                    pass
                att.close()
            if server is not None:
                stop_iperf3_server(server); self.log("iperf3 서버 종료")
            sys.stdout = old_stdout
            self.q.put(("finished", None))

    # ---------------- 큐 폴링(메인 스레드) ----------------
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    self.status.set(payload)
                elif kind == "result":
                    self._add_result(payload)
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "error":
                    messagebox.showerror("측정 오류", payload)
                elif kind == "finished":
                    self._reset_buttons()
                elif kind == "precheck_done":
                    self.btn_check.config(state="normal")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _add_result(self, r):
        self.results.append(r)
        rssi = f"{r['rssi']:.1f}" if isinstance(r["rssi"], (int, float)) else "N/A"
        self.tree.insert("", "end", values=(f"{r['target']:.0f}", f"{r['att']:.2f}",
                         rssi, r["dl"], r["ul"], r["status"]))
        self._redraw_graph()

    def _redraw_graph(self):
        rows = sorted([r for r in self.results
                       if isinstance(r["rssi"], (int, float))], key=lambda x: x["rssi"])
        if not rows:
            return
        self._init_axes()
        x = [r["rssi"] for r in rows]
        dl = [r["dl"] for r in rows]
        ul = [r["ul"] for r in rows]
        self.ax.plot(x, dl, "o-", color="#1f77b4", lw=2, ms=7, label="DL")
        if any(u > 0 for u in ul):
            self.ax.plot(x, ul, "s--", color="#d62728", lw=2, ms=6, label="UL")
        for xi, yi in zip(x, dl):
            self.ax.annotate(f"{yi:g}", (xi, yi), textcoords="offset points",
                             xytext=(0, 7), ha="center", fontsize=8, color="#1f77b4")
        self.ax.set_ylim(bottom=0)
        self.ax.invert_xaxis()
        self.ax.legend()
        self.fig.tight_layout()
        self.canvas.draw()

    def _on_done(self, csv_path):
        png = os.path.splitext(csv_path)[0] + ".png"
        try:
            self.fig.savefig(png, dpi=130)
            self.log(f"그래프 저장: {png}")
        except Exception as e:
            self.log(f"(그래프 저장 실패: {e})")
        self.status.set(f"완료 — CSV: {csv_path}")
        messagebox.showinfo("측정 완료",
                            f"CSV: {csv_path}\nPNG: {png}\n포인트 {len(self.results)}개 측정 완료")

    def _reset_buttons(self):
        self.btn_start.config(state="normal")
        self.btn_check.config(state="normal")
        self.btn_stop.config(state="disabled")

    def _open_results(self):
        os.makedirs(RESULTS_DIR, exist_ok=True)
        os.startfile(os.path.abspath(RESULTS_DIR))

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel("종료", "측정이 진행 중입니다. 중지하고 종료할까요?"):
                return
            self.stop_event.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
