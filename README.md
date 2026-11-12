# ⏱️ NTP Stratum Simulation (Enhanced Visual TUI with Chaos Fault Injection)

Simulasi **Network Time Protocol (RFC 5905)** berbasis arsitektur **Hierarki Stratum (Stratum 1, 2, dan 3)** dengan **Interactive Terminal User Interface (TUI)** modern, **Multi-Peer Cross Validation**, **Slew vs Step Dynamic Progress**, dan **Chaos Fault Injection** untuk tugas kelompok mata kuliah Sistem Terdistribusi (6 node/laptop).

![Language](https://img.shields.io/badge/Language-Python%203.8+-blue.svg)
![Interface](https://img.shields.io/badge/Interface-Rich%20TUI%20Dashboard-brightgreen.svg)
![Fault-Tolerance](https://img.shields.io/badge/Fault--Tolerance-Marzullo%20Algorithm-orange.svg)

---

## 🌟 Fitur Baru & Peningkatan Visual

### 1. 🖥️ Deteksi & Tampilan IP Lokal Otomatis
- Header sekarang langsung menampilkan **Alamat IP LAN laptop**:
  `IP: 192.168.1.45:5000 (hostname: bagas-laptop)`
- Sangat mempermudah koordinasi saat demo di kelas; anggota lain langsung tahu IP mana yang harus dimasukkan ke `--upstream` tanpa perlu buka `ipconfig` atau `ifconfig`.

### 2. 🌊 Perbedaan Nyata `SLEW` vs `STEP` Mode
- **`STEP` (Instant Jump):** Jam langsung melompat seketika ke waktu baru. Banner menampilkan `⚡ INSTANT STEP APPLIED: Clock shifted +14.2s`.
- **`SLEW` (Gradual Adaptation):** Jam tidak melompat (menjaga konsistensi sistem file & DB). Jam beradaptasi secara gradual ($\pm 0.40\text{ s/s}$) lengkap dengan **Animated Progress Bar**:
  `🌊 SLEWING SMOOTHLY: [████████████░░░░░░] 68% (Left: +3.200s / Total: +14.200s)`

### 3. 📉 Random Hardware Drift (Bisa Positif dan Negatif)
- Tekan **`[D]`**: Osilator kuarsa fisik akan meleset secara acak, bisa **melambat** maupun **mempercepat**:
  - `📈 DRIFT: +18.4ms/s (FAST)`
  - `📉 DRIFT: -14.2ms/s (SLOW)`

### 4. 🎲 Falseticker: Manual vs Auto-Chaos (Random Occurrence)
- **`[F]` - Manual Falseticker:** Menyuntikkan distorsi waktu tetap $+60.0$ detik secara konstan. Layar membelah menampilkan jam asli vs jam palsu yang disiarkan.
- **`[C]` - Auto Chaos Mode (Random Glitches):** Mengaktifkan gangguan acak intermittant (peluang 35% di tiap request menghasilkan lonjakan skew acak $\pm 30\text{s}$ s.d. $\pm 90\text{s}$).

### 5. 📊 Elemen Visual Tambahan
- **Visual Stratum Hierarchy Map:**
  `[ Stratum 1 Master ] ──UDP──> [ STRATUM 2: THIS RELAY ] ──UDP──> [ Stratum 3 Leaf Clients ]`
- **Offset Sparkline Trend:**
  Grafik mini Unicode yang memvisualisasikan penurunan offset dari waktu ke waktu: `█▆▅▃▂     `.
- **Packet Counters:** Menampilkan jumlah total paket request yang terkirim (`TX`) dan diterima (`RX`).

---

## 🎮 Hotkeys Interaktif (Tanpa Perlu Tekan Enter)

| Tombol | Fungsi | Keterangan |
| :---: | :--- | :--- |
| **`[F]`** | **Toggle Manual Falseticker** | Menyuntikkan kesalahan waktu $+60$ detik secara sengaja. |
| **`[C]`** | **Toggle Auto-Chaos Mode** | Mengaktifkan gangguan waktu acak sporadis (35% probability). |
| **`[M]`** | **Toggle Slew vs Step** | Beralih antara lompatan instan (`STEP`) atau penyesuaian halus (`SLEW`). |
| **`[D]`** | **Toggle Random Drift** | Mengaktifkan/menonaktifkan drift acak ($\pm \text{ms/s}$). |
| **`[Q]`** | **Quit** | Keluar dari aplikasi dengan bersih. |

---

## 🚀 Instalasi & Persiapan

```bash
git clone https://github.com/jempolbagas/ntp-simulation.git
cd ntp-simulation
pip install -r requirements.txt
```

---

## 💻 Panduan Menjalankan

### Skenario Demo 6 Laptop (Wi-Fi / LAN / Tailscale)

Catat IP Laptop 1, 2, dan 3 yang tampil di header masing-masing laptop:
- Misal IP Laptop 1 (Stratum 1): `192.168.1.10`
- Misal IP Laptop 2 (Stratum 2A): `192.168.1.11`
- Misal IP Laptop 3 (Stratum 2B): `192.168.1.12`

Jalankan di masing-masing laptop:

| Laptop | Peran | Perintah Eksekusi |
| :--- | :--- | :--- |
| **Laptop 1** | **Stratum 1 (Master)** | `python3 ntp_node.py --stratum 1 --port 5000` |
| **Laptop 2** | **Stratum 2A (Relay)** | `python3 ntp_node.py --stratum 2 --port 5000 --upstream 192.168.1.10:5000` |
| **Laptop 3** | **Stratum 2B (Relay)** | `python3 ntp_node.py --stratum 2 --port 5000 --upstream 192.168.1.10:5000` |
| **Laptop 4** | **Stratum 3 (Client 1)** | `python3 ntp_node.py --stratum 3 --upstream 192.168.1.11:5000 192.168.1.12:5000` |
| **Laptop 5** | **Stratum 3 (Client 2)** | `python3 ntp_node.py --stratum 3 --upstream 192.168.1.11:5000 192.168.1.12:5000` |
| **Laptop 6** | **Stratum 3 (Client 3)** | `python3 ntp_node.py --stratum 3 --upstream 192.168.1.11:5000 192.168.1.12:5000` |

---

## 📐 Rumus Matematis NTP

$$\delta = (T_4 - T_1) - (T_3 - T_2) \quad \text{(Round-Trip Delay)}$$

$$\theta = \frac{(T_2 - T_1) + (T_3 - T_4)}{2} \quad \text{(Clock Offset)}$$

- **Step Mode:** $Waktu_{\text{baru}} = Waktu_{\text{saat\_ini}} + \theta$
- **Slew Mode:** Penyesuaian bertahap $\pm 0.40\text{ s/s}$ hingga target $\theta$ tercapai tanpa merusak timer aplikasi.
