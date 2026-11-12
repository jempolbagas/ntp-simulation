# NTP Stratum Simulation & Authentic Marzullo Engine

Simulasi **Network Time Protocol (RFC 5905)** berbasis arsitektur **Hierarki Stratum (Stratum 1, 2, dan 3)** dengan **Terminal User Interface (TUI)** berstandar engineering, implementasi autentik **Algoritma Marzullo** untuk penolakan falseticker (outlier rejection), **VirtualClock** dengan dynamic Brownian quartz drift dan step/slew adjustment, serta **Tmux Local Cluster Runner** untuk pengujian multi-node.

![Python](https://img.shields.io/badge/Python-3.8%2B-blue.svg)
![Interface](https://img.shields.io/badge/Interface-Rich%20TUI%20Dashboard-brightgreen.svg)
![Algorithm](https://img.shields.io/badge/Algorithm-Authentic%20Marzullo-orange.svg)
![Tests](https://img.shields.io/badge/Tests-Pytest%20Suite-success.svg)
![Architecture](https://img.shields.io/badge/Architecture-Modular%20Package-purple.svg)

---

## Daftar Isi
- [Arsitektur Sistem](#arsitektur-sistem)
- [Matematika Sinkronisasi & Algoritma Marzullo](#matematika-sinkronisasi--algoritma-marzullo)
- [Dinamika VirtualClock (Drift, Step, & Slew)](#dinamika-virtualclock-drift-step--slew)
- [Instalasi & Persiapan](#instalasi--persiapan)
- [Penggunaan CLI (Single Node)](#penggunaan-cli-single-node)
- [Local Cluster Runner (tmux)](#local-cluster-runner-tmux)
- [Hotkey Interaktif & Fault Injection](#hotkey-interaktif--fault-injection)
- [Pengujian & Verifikasi (Pytest)](#pengujian--verifikasi-pytest)

---

## Arsitektur Sistem

Proyek ini telah direfaktor secara penuh menjadi package Python modular `ntp_simulation/` yang bersih dan decoupled:

```
ntp-simulation/
├── ntp_simulation/
│   ├── __init__.py               # Package metadata & version
│   ├── __main__.py               # Entrypoint modul: python3 -m ntp_simulation
│   ├── cli.py                    # Argument parsing, lifecycle coordination, background workers
│   ├── state.py                  # Thread-safe telemetry store (SharedState & Snapshot)
│   ├── models/                   # Pydantic schemas
│   │   ├── packet.py             # NTPRequest & NTPResponse dengan cryptographic nonce
│   │   ├── peer.py               # PeerAddress, PeerSample, MarzulloEvaluation
│   │   └── config.py             # NodeConfig runtime validation
│   ├── algorithms/
│   │   └── marzullo.py           # Authentic Marzullo Intersection Algorithm (sweep-line)
│   ├── clock/
│   │   └── virtual_clock.py      # Thread-safe timekeeper, Brownian drift, step/slew dynamics
│   ├── network/
│   │   ├── client.py             # Concurrent UDP client dengan ThreadPoolExecutor & nonce matching
│   │   ├── server.py             # UDP listener dengan fault injection (+60s skew & 35% chaos)
│   │   └── socket_utils.py       # Socket bindings & local IP address resolution
│   └── ui/
│       ├── key_reader.py         # Signal-safe POSIX non-blocking key reader (atexit/SIGINT cleanup)
│       ├── widgets.py            # Rich gauges, sparklines, dan telemetry tables
│       └── dashboard.py          # 4-section layout (Header, Clocks, Main body, Footer)
├── scripts/
│   └── run_cluster_tmux.sh       # 6-node local cluster orchestrator (tiled tmux grid)
├── tests/                        # Comprehensive automated test suite
├── requirements.txt              # rich>=13.0.0, pydantic>=2.0.0, pytest>=7.0.0
└── README.md                     # Dokumentasi teknis sistem
```

### Topologi Hierarki Stratum

```
               +-------------------------------------+
               |       Stratum 1 (Master / UTC)      |
               |             Port: 5001              |
               +-------------------------------------+
                                  |
                 +----------------+----------------+
                 |                                 |
                 v                                 v
   +---------------------------+     +---------------------------+
   |   Stratum 2 (Relay A)     |     |   Stratum 2 (Relay B)     |
   |        Port: 5002         |     |        Port: 5003         |
   |   Upstream: :5001         |     |   Upstream: :5001         |
   +---------------------------+     +---------------------------+
                 \                 / \                 /
                  \               /   \               /
                   v             v     v             v
             +--------------------+   +--------------------+   +--------------------+
             | Stratum 3 Client A |   | Stratum 3 Client B |   | Stratum 3 Client C |
             |    Port: 5004      |   |    Port: 5005      |   |    Port: 5006      |
             | Upstream: :5002,   |   | Upstream: :5002,   |   | Upstream: :5002,   |
             |           :5003    |   |           :5003    |   |           :5003    |
             +--------------------+   +--------------------+   +--------------------+
```

---

## Matematika Sinkronisasi & Algoritma Marzullo

### 1. Estimasi Offset dan Round-Trip Delay (RFC 5905)
Untuk setiap transaksi pertukaran paket UDP antara client dan peer:
- $T_1$: Waktu client mengirim `NTPRequest`.
- $T_2$: Waktu server menerima request.
- $T_3$: Waktu server mengirimkan `NTPResponse`.
- $T_4$: Waktu client menerima respons.

Offset waktu ($\theta$) dan round-trip delay ($\delta$) dihitung secara presisi:
$$\theta = \frac{(T_2 - T_1) + (T_3 - T_4)}{2}$$
$$\delta = (T_4 - T_1) - (T_3 - T_2)$$

### 2. Confidence Interval, Dispersion Floor & Sweep-Line Algorithm
Untuk setiap peer $i$, dibangun interval keyakinan dengan batas toleransi dispersi (*dispersion floor* $\epsilon_{\text{floor}}$, default 50ms sesuai RFC 5905):
$$I_i = \left[ \theta_i - \left(\frac{\delta_i}{2} + \epsilon_{\text{floor}}\right), \; \theta_i + \left(\frac{\delta_i}{2} + \epsilon_{\text{floor}}\right) \right]$$

Penggunaan $\epsilon_{\text{floor}}$ mencegah *false positive* pada jaringan lokal / localhost berlatensi sangat rendah ($\delta < 1\text{ms}$), sehingga osilator kuarsa dengan fluktuasi normal ($\pm 5-20\text{ms}$) tetap dapat membentuk irisan konsensus, sementara falseticker dengan kesalahan drastis ($\pm 60\text{s}$) tetap tereliminasi secara mutlak.

Algoritma Marzullo membangun tabel titik batas (endpoints):
- Titik awal interval: $( \theta_i - (\delta_i/2 + \epsilon_{\text{floor}}), \; -1 )$
- Titik akhir interval: $( \theta_i + (\delta_i/2 + \epsilon_{\text{floor}}), \; +1 )$

Tabel diurutkan berdasarkan nilai offset secara menaik. Jika terdapat koordinat yang bernilai sama, titik awal ($-1$) ditempatkan lebih dahulu daripada titik akhir ($+1$) untuk menjamin perhitungan irisan yang konsisten:
- Algoritma melakukan penyisiran (*sweep-line*) terhadap semua titik untuk mencari irisan terbesar yang disepakati oleh mayoritas sumber waktu (*Truechimers*).
- Peer yang intervalnya berada di luar wilayah konsensus diklasifikasikan sebagai **Falseticker** dan dieliminasi dari kalkulasi penyesuaian jam.
- Dari kumpulan *Truechimers*, dipilih satu peer terbaik yang memiliki **round-trip delay terkecil ($\min \delta_i$)** sebagai referensi sinkronisasi.

---

## Dinamika VirtualClock (Drift, Step, & Slew)

Jam virtual (`VirtualClock`) mengimplementasikan model jam fisik yang realistis:

1. **Dynamic Brownian Quartz Drift:**
   - Mensimulasikan fluktuasi termal dan ketidakstabilan osilator kuarsa fisik.
   - Drift bergerak secara *random walk* dalam rentang $\pm 25\text{ ms/s}$ dengan penghalusan eksponensial.
2. **Step Mode (Instantaneous Jump):**
   - Offset diaplikasikan seketika: $\Delta t = \theta$.
   - Cocok untuk koreksi awal saat deviasi waktu sangat besar.
3. **Slew Mode (Continuous Frequency Slewing):**
   - Offset disesuaikan secara bertahap dengan laju konstan $0.40\text{ s/s}$.
   - Mencegah terjadinya pembalikan waktu (*monotonicity violation*), menjaga keandalan database dan log sistem operasi.

---

## Instalasi & Persiapan

### Dependensi
- Python 3.8+
- Tmux (untuk cluster local multi-node)

### Langkah Instalasi
```bash
git clone https://github.com/jempolbagas/ntp-simulation.git
cd ntp-simulation
pip install -r requirements.txt
```

Isi `requirements.txt`:
```
rich>=13.0.0
pydantic>=2.0.0
pytest>=7.0.0
```

---

## Penggunaan CLI (Single Node)

Eksekusi node dilakukan langsung melalui package Python:
```bash
python3 -m ntp_simulation --help
```

### Tabel Parameter CLI

| Opsi | Tipe | Default | Keterangan |
| :--- | :---: | :---: | :--- |
| `--stratum` | `int` | *Wajib* | Level stratum node (`1`=Master, `2`=Relay, `3`=Client). |
| `--port` | `int` | `5000` | Port UDP listening server. |
| `--bind-host` | `str` | `0.0.0.0` | Interface IP untuk bind UDP server. |
| `--upstream` | `str...` | `[]` | Satu atau lebih alamat upstream (`HOST:PORT`). Wajib untuk Stratum > 1. |
| `--interval` | `float` | `4.0` | Interval polling sinkronisasi dalam detik. |
| `--no-ui` | `flag` | `False` | Jalankan dalam headless mode (output log ke stdout tanpa TUI). |
| `--sync-mode` | `choice` | `STEP` | Mode penyesuaian jam awal (`STEP` atau `SLEW`). |
| `--drift` | `flag` | `False` | Aktifkan Brownian hardware drift sejak startup. |
| `--falseticker` | `flag` | `False` | Mulai node dalam fault mode manual falseticker (+60s skew). |
| `--chaos` | `flag` | `False` | Mulai node dalam fault mode auto chaos (35% sporadic glitches). |
| `--dispersion-floor` | `float` | `0.050` | Batas toleransi ketidakpastian/dispersion floor (RFC 5905) dalam detik (50ms). |
| `--initial-offset` | `float` | `None` | Offset awal jam dalam detik (default: acak ±5-30s untuk Stratum > 1, 0s untuk Stratum 1). |

### Contoh Eksekusi Mandiri

**1. Stratum 1 Master:**
```bash
python3 -m ntp_simulation --stratum 1 --port 5001
```

**2. Stratum 2 Relay (merujuk ke Stratum 1):**
```bash
python3 -m ntp_simulation --stratum 2 --port 5002 --upstream 127.0.0.1:5001
```

**3. Stratum 3 Client (merujuk ke Relay A & Relay B):**
```bash
python3 -m ntp_simulation --stratum 3 --port 5004 --upstream 127.0.0.1:5002 127.0.0.1:5003
```

---

## Local Cluster Runner (tmux)

Tersedia script otomatis `scripts/run_cluster_tmux.sh` yang mendirikan cluster 6 node lengkap dalam grid 6-pane tmux:

```bash
./scripts/run_cluster_tmux.sh start
```

### Pemetaan Pane Tmux
- **Pane 0:** Stratum 1 Master (`:5001`)
- **Pane 1:** Stratum 2 Relay A (`:5002` -> `127.0.0.1:5001`)
- **Pane 2:** Stratum 2 Relay B (`:5003` -> `127.0.0.1:5001`)
- **Pane 3:** Stratum 3 Client A (`:5004` -> `127.0.0.1:5002`, `127.0.0.1:5003`)
- **Pane 4:** Stratum 3 Client B (`:5005` -> `127.0.0.1:5002`, `127.0.0.1:5003`)
- **Pane 5:** Stratum 3 Client C (`:5006` -> `127.0.0.1:5002`, `127.0.0.1:5003`)

### Subcommand `run_cluster_tmux.sh`

| Perintah | Deskripsi |
| :--- | :--- |
| `./scripts/run_cluster_tmux.sh start` | Menjalankan seluruh cluster 6 node di tmux session (otomatis attach jika interaktif). |
| `./scripts/run_cluster_tmux.sh start -d` | Menjalankan cluster dalam mode detached di latar belakang. |
| `./scripts/run_cluster_tmux.sh status` | Memeriksa status sesi cluster aktif atau nonaktif. |
| `./scripts/run_cluster_tmux.sh attach` | Menghubungkan terminal kembali ke sesi cluster yang sedang berjalan. |
| `./scripts/run_cluster_tmux.sh restart` | Mematikan dan memulai ulang sesi cluster secara bersih. |
| `./scripts/run_cluster_tmux.sh stop` | Mematikan sesi cluster dan seluruh node di dalamnya. |

### Navigasi Tmux
- Pindah antar-pane: Tekan **`Ctrl+B`**, lepas, lalu tekan tombol panah (`Up`/`Down`/`Left`/`Right`).
- Detach dari sesi (biarkan cluster tetap jalan di background): Tekan **`Ctrl+B`**, lalu tekan **`D`**.

---

## Hotkey Interaktif & Fault Injection

Setiap node yang berjalan dengan Rich TUI menyediakan kontrol interaktif real-time menggunakan keyboard:

| Hotkey | Aksi | Deskripsi Teknis |
| :---: | :--- | :--- |
| **`[SPACE]`** | **Freeze / Unfreeze UI** | Membekukan tampilan telemetry di layar untuk inspeksi saat presentasi. *Background workers (polling UDP, kalkulasi Marzullo, dan sinkronisasi jam) tetap berjalan penuh tanpa terhenti.* |
| **`[F]`** | **Manual Falseticker** | Menyuntikkan distorsi waktu $+60.0$ detik secara konstan pada server UDP node ini. Amati bagaimana client upstream menolak node ini sebagai *Falseticker*. |
| **`[C]`** | **Auto Chaos Mode** | Mengaktifkan gangguan acak sporadis (probabilitas 35% pada setiap paket request menghasilkan lonjakan skew acak $\pm 30\text{s}$ hingga $\pm 90\text{s}$). |
| **`[M]`** | **Toggle STEP / SLEW** | Beralih antara mode adaptasi instan (`STEP`) dan adaptasi gradual berkecepatan $0.40\text{ s/s}$ (`SLEW`). |
| **`[D]`** | **Dynamic Quartz Drift** | Mengaktifkan atau menonaktifkan simulasi fluktuasi osilator kuarsa Brownian ($\pm 25\text{ ms/s}$). |
| **`[Q]`** | **Graceful Shutdown** | Menghentikan seluruh worker thread dan mengembalikan terminal ke mode standar secara bersih. |

---

## Pengujian & Verifikasi (Pytest)

Suite pengujian otomatis berbasis `pytest` mencakup pengujian unit dan integrasi untuk seluruh subsistem:

```bash
# Menjalankan seluruh test suite
pytest -v

# Menjalankan modul pengujian spesifik
pytest tests/test_marzullo.py -v
pytest tests/test_clock.py -v
pytest tests/test_models.py -v
pytest tests/test_network.py -v
pytest tests/test_cli_cluster.py -v
pytest tests/test_state_concurrency.py -v
```

### Cakupan Pengujian
1. **Marzullo Algorithm:** Verifikasi interval overlap, tie-breaking endpoint sorting, konsensus mayoritas, serta eliminasi falseticker pada berbagai variasi skenario kegagalan.
2. **VirtualClock Engine:** Verifikasi perhitungan waktu thread-safe, laju adaptasi slewing ($0.40\text{ s/s}$), instantaneous step, dan batasan random-walk Brownian drift.
3. **Data Models & Nonce:** Validasi skema Pydantic untuk request/response UDP, validasi rentang stratum, serialisasi JSON, dan pencocokan correlation nonce.
4. **Network & Concurrency:** Pengujian concurrent UDP polling, toleransi packet drop, server listener, dan fault injection.
5. **Cluster & Runner:** Verifikasi script runner tmux, perintah subcommands, dan integritas konfigurasi port cluster.
