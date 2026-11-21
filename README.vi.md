<div align="right">

[![English](https://img.shields.io/badge/English-read%20in%20English-1f6feb)](README.md)
[![Tiếng Việt](https://img.shields.io/badge/Ti%E1%BA%BFng%20Vi%E1%BB%87t-%C4%91ang%20ch%E1%BB%8Dn-da3633)](README.vi.md)

</div>

# AI Stick

Chạy Debian trên một chiếc USB 4G giá 10 đô (Qualcomm MSM8916), để làm nhà cho một trợ lý AI cá nhân mà bạn nói chuyện qua Telegram.

> [!WARNING]
> **Đang làm dở.** Flash sẽ xoá firmware gốc và có thể làm chết dongle: hãy sao lưu toàn bộ eMMC trước.

## Tình trạng

Đã thử trên một dongle JZ01-45-V33 (2026-11-13), dùng image do workflow của repo này build và flash qua EDL từ Windows:

- Boot được Debian 13 (trixie), kernel 6.12.1, và lên lại bình thường sau khi reboot.
- WiFi access point, mạng qua USB (RNDIS), SSH và trang admin đều chạy.
- Modem được nhận, giữ nguyên IMEI gốc. LTE data và SMS **chưa thử** (chưa cắm SIM).
- Stick có thể vào một mạng WiFi 2.4 GHz sẵn có thay vì tự phát WiFi: vào **WiFi → Join a WiFi network** trên trang admin. Nếu vào mạng không được, access point sẽ được bật lại. Cách này không cần SIM.
- Trợ lý AI chưa nằm trong image. Trên stick thử nghiệm, nó được cài riêng (ZeroClaw + một bot Telegram), nói chuyện với model server qua [`router/`](router/README.md).

## Ngoài bộ build image, repo còn có gì

| Thư mục | Là gì |
|---|---|
| [`admin/`](admin/README.md) | Trang admin trên web. Dùng để vào mạng WiFi; tab **Assistant** cho xem điện áp USB, mạng, lưu lượng gọi model, các service, đèn và log trong ngày. Chỉ trả lời qua cáp USB và mạng LAN trong nhà, không bao giờ trả lời qua VPN hay địa chỉ public |
| [`router/`](router/README.md) | `stick-router`: một endpoint chuẩn OpenAI đứng trước nhiều nhà cung cấp model, có fallback. Có thể ép một model nhỏ gọi một tool cho trước ở đầu mỗi lượt người dùng nói. Chỉ là một file Go 6 MB, tốn khoảng 13 MB RAM |
| [`leds/`](leds/stick-leds.py) | Đèn trạng thái: xanh dương = mạng, xanh lá = trợ lý đang bận, đỏ = có lỗi, xanh dương và đỏ nháy luân phiên = đang cập nhật |
| [`scripts/reduce-writes.sh`](scripts/reduce-writes.sh) | Chuyển journal lên RAM và giãn việc ghi xuống filesystem. Trên stick thử nghiệm, lượng ghi lúc rảnh vào eMMC giảm từ khoảng 2 MB xuống khoảng 90 KB mỗi phút |
| [`flash/`](flash/flash_edl.py) | Flash image qua EDL, không cần fastboot |

## Phần cứng nhắm tới

| | |
|---|---|
| Bo mạch | `JZ01-45-V33` (in trên mạch `JZ0145_V33`, tem dán `JZ01-45-@`), bán dưới tên dongle USB wifi "4G-UFI" |
| SoC | Qualcomm MSM8916, HWID `0x007050e1` |
| Bộ nhớ | eMMC 4 GB (7,733,248 sector) |
| Firmware gốc | Android 4.4.4, giao diện web `UFI-JZ_V3.0.0` tại `192.168.100.1` |

Các bo mạch khác mà bộ build hỗ trợ: `uz801`, `ufi001c` (chưa thử ở đây).

## Build

Chạy workflow **Build** trong tab Actions và chọn bo mạch (mặc định `jz01-45-v33`). Các file firmware được đính kèm vào lần chạy dưới dạng artifact.

Build trên máy, dùng Ubuntu:

```shell
git clone --recurse-submodules https://github.com/duycuf/ai-stick.git
cd ai-stick
sudo BOARD=jz01-45-v33 ./build.sh
```

## Flash qua EDL (không cần fastboot)

[`flash/flash_edl.py`](flash/flash_edl.py) ghi image đã build qua Qualcomm EDL bằng [bkerler/edl](https://github.com/bkerler/edl). Script tạo bảng phân vùng cho toàn bộ eMMC theo đúng dung lượng của bạn, bung file sparse của `boot` và `rootfs`, lưu lại các phân vùng riêng của từng máy ngay trước khi flash, ghi tất cả, rồi đọc lại từng lần ghi để so sánh.

```shell
# files/ holds the build artifact. Nothing is written to the device by these two:
python flash/flash_edl.py prepare --sectors 7733248   # eMMC size from `edl printgpt`
python flash/flash_edl.py plan                        # prints every edl command

# with the dongle in EDL mode (05c6:9008):
python flash/flash_edl.py flash --edl "python C:/edl/edl.py" \
    --loader C:/edl/Loaders/prog_emmc_firehose_8916.mbn --yes-erase-device
```

Ghi chú khi làm việc này trên Windows 11:

- Gán `QHSUSB__BULK` cho **WinUSB** bằng Zadig. Với libusb-win32, việc đọc bị treo và mất dữ liệu.
- Vào EDL bằng `adb reboot edl` từ firmware gốc, hoặc giữ nút trong lúc cắm dongle vào. Hãy kiểm tra cách giữ nút có chạy không trước khi xoá firmware gốc: sau khi xoá, đó là đường duy nhất để quay lại.
- `edl` chỉ chạy được một lệnh mỗi phiên EDL, cho tới khi bước dò chế độ Sahara/Firehose được cho thêm thời gian để trả lời (một bản vá nhỏ trên máy cho `edlclient/Library/sahara.py`).
- Sau lần ghi cuối, có thể phải tự chạy lại `edl reset` thêm một lần.

## Ghi công

Repo này không phải là fork trên GitHub, nhưng được xây dựng trên công sức của người khác theo giấy phép MIT (xem [LICENSE](LICENSE)):

- [kinsamanka/OpenStick-Builder](https://github.com/kinsamanka/OpenStick-Builder): bộ build image gốc.
- [Mio-sha512](https://github.com/Mio-sha512/OpenStick-Builder) và [dbrant](https://github.com/dbrant/OpenStick-Builder): phần cài đặt hotspot, tor, nftables và hostapd.
- [lastbest/OpenStick-Builder](https://github.com/lastbest/OpenStick-Builder): tuỳ chọn `BOARD=`, bản vá lk2nd và mọi bản sửa cho bo mạch JZ01-45-V33, trang admin, và [docs/UF902_JZ01_NOTES.md](docs/UF902_JZ01_NOTES.md). Mã nguồn ở đây bắt đầu từ nhánh `main` của repo đó, tại commit `1b2b765`.
- [msm8916-mainline](https://github.com/msm8916-mainline) (lk2nd, qhypstub, qtestsign), [postmarketOS](https://postmarketos.org/) (kernel) và dự án [OpenStick](https://github.com/OpenStick/OpenStick).

---

# Tài liệu của bộ build

Phần còn lại của file này là tài liệu gốc của bộ build, giữ lại từ upstream. Các URL clone và tên nhánh trong đó là của các repo upstream.

Bộ build này dùng [kernel](https://pkgs.postmarketos.org/package/master/postmarketos/aarch64/linux-postmarketos-qcom-msm8916) biên dịch sẵn do [postmarketOS](https://postmarketos.org/) cung cấp cho các thiết bị Qualcomm MSM8916.

## Hướng dẫn build
### Build trên máy
Đã chạy được trên **Ubuntu 22.04, 24.04 và 25.04**
- clone
  ```shell
  git clone --recurse-submodules https://github.com/Mio-sha512/OpenStick-Builder.git
  cd OpenStick-Builder/
  ```
#### Nhanh (nhánh Debian)
```shell
git checkout debian
sudo ./build.sh
```
#### Nhanh (nhánh Ubuntu 24.04)
```shell
git checkout ubuntu-24.04
sudo ./build.sh
```
#### Chọn bo mạch
lk1st (bản build lk2nd dùng làm `aboot`) không tự nhận ra được các dongle này, nên
bo mạch sẽ quyết định lk2nd theo dõi GPIO nút bấm nào và devicetree kernel nào được
ghi vào `extlinux.conf`. Truyền `BOARD=` khi build (mặc định `uz801`):

| `BOARD=` | Bo mạch | lk2nd compatible | kernel DTB |
| --- | --- | --- | --- |
| `uz801` (mặc định) | YiMing UZ801 v3 | `yiming,uz801-v3` | `msm8916-yiming-uz801v3` |
| `jz01-45-v33` | Dongle UFI "JZ" JZ01-45-V33 (ví dụ UF902) | `openstick,jz01-45-v33` | `msm8916-jz01-45-v33` |
| `ufi001c` | UFI-001B/C | `thwc,ufi001c` | `msm8916-thwc-ufi001c` |

```shell
sudo BOARD=jz01-45-v33 ./build.sh
```
> [!IMPORTANT]
> Build một bo mạch không phải UZ801 với lk2nd mặc định `uz801` sẽ khiến lk2nd đọc
> nhầm GPIO và tưởng nút đang được giữ. Trên JZ01-45-V33, mọi lần boot sẽ bị đẩy vào
> recovery/fastboot (`ERROR: No recovery partition found`), nên Linux không bao giờ khởi động.

Xem [docs/UF902_JZ01_NOTES.md](docs/UF902_JZ01_NOTES.md) để đọc bài viết đầy đủ về UF902 / JZ01-45-V33.

#### Chi tiết (giống nhau cho mọi nhánh)
- cài các gói phụ thuộc
  ```shell
  sudo scripts/install_deps.sh
  ```
- build hyp và lk2nd

  Hai bootloader tuỳ biến này hỗ trợ cơ bản file `extlinux.conf`, giống U-Boot và Depthcharge.
  ```shell
  sudo scripts/build_hyp_aboot.sh
  ```
- trích xuất firmware Qualcomm

  Lấy bootloader ra và tạo bảng phân vùng mới dùng hết dung lượng eMMC
  ```shell
  sudo scripts/extract_fw.sh
  ```
- tạo rootfs bằng debootstrap
  ```shell
  sudo scripts/debootstrap.sh
  ```

- build gadget-tools
  ```shell
  sudo scripts/build_gt.sh
  ```
- tạo image
  ```shell
  sudo scripts/build_images.sh
  ```

Các file firmware tạo ra sẽ nằm trong thư mục `files`

### Build trên cloud bằng Github Actions
1. Fork repo này
2. Chạy [Build workflow](../../actions/workflows/build.yml)
   - bấm vào và chạy ***Run workflow***
   - khi workflow chạy xong, bấm vào phần tóm tắt của workflow rồi tải artifact kết quả về

## Trang admin
Image có kèm một trang admin web nhỏ tại **http://192.168.100.1/** (trạng thái LTE, WiFi,
APN, các máy đang kết nối, reboot …), xem [`admin/`](admin/README.md). Build với `WITH_ADMIN=0` để
bỏ nó ra, hoặc chạy `admin/install.sh` trên stick đã flash sẵn.

## Tuỳ biến
Sửa [`scripts/setup.sh`](scripts/setup.sh) để thêm/bớt gói. Lưu ý script này chạy bên trong môi trường `chroot`.

## Cài firmware
> [!WARNING]  
> Các lệnh dưới đây có thể làm chết thiết bị, khiến nó không boot được nữa. Hãy cẩn thận và tự chịu rủi ro!

> [!IMPORTANT]  
> Nhớ sao lưu firmware gốc bằng lệnh `edl rf orig_fw.bin`

### Cần chuẩn bị
- [EDL](https://github.com/bkerler/edl)
- Công cụ fastboot của Android
  ```
  sudo apt install fastboot
  ```

### Các bước
- Đảm bảo thiết bị đang chạy firmware gốc
- Vào chế độ Qualcomm EDL theo [hướng dẫn](https://wiki.postmarketos.org/wiki/Zhihe_series_LTE_dongles_(generic-zhihe)#How_to_enter_flash_mode) này
- Sao lưu các phân vùng cần thiết

  Cần các file sau từ firmware gốc:

     - `fsc.bin`
     - `fsg.bin`
     - `modem.bin`
     - `modemst1.bin`
     - `modemst2.bin`
     - `persist.bin`
     - `sec.bin`

  Bỏ qua bước này nếu đã có sẵn các file này
  ```shell
  for n in fsc fsg modem modemst1 modemst2 persist sec; do
      edl r ${n} ${n}.bin
  done
  ```
- Cài `aboot`
  ```shell
  edl w aboot aboot.mbn
  ```
- Reboot vào fastboot
  ```shell
  edl e boot
  edl reset
  ```
- Flash firmware
  ```shell
  fastboot flash partition gpt_both0.bin
  fastboot flash aboot aboot.mbn
  fastboot flash hyp hyp.mbn
  fastboot flash rpm rpm.mbn
  fastboot flash sbl1 sbl1.mbn
  fastboot flash tz tz.mbn
  fastboot flash boot boot.bin
  fastboot flash rootfs rootfs.bin
  ```
- Khôi phục các phân vùng gốc
  ```shell
  for n in fsc fsg modem modemst1 modemst2 persist sec; do
      fastboot flash ${n} ${n}.bin
  done
  ```
- Reboot
  ```shell
  fastboot reboot
  ```

## Sau khi cài
- Cấu hình mạng
  
  |  | |
  | ----- | ---- |
  | ssid | 4G-UFI-XX |
  | mật khẩu | 1234567890 |
  | địa chỉ ip | 192.168.100.1 |

- Tài khoản mặc định
  
  | | |
  | ----- | ---- |
  | tên đăng nhập | user |
  | mật khẩu | 1 |
 
- Nếu bạn build với `BOARD=uz801` mặc định nhưng thiết bị không phải **UZ801**, hãy sửa `/boot/extlinux/extlinux.conf` để dùng đúng devicetree (tốt hơn: build lại với `BOARD=` đúng, xem ở trên)
  ```shell
  sed -i 's/yiming-uz801v3/<BOARD>/' /boot/extlinux/extlinux.conf
  ```

  trong đó `<BOARD>` là
     - `thwc-uf896` cho bo mạch **UF896**
     - `thwc-ufi001c` cho bo mạch **UFIxxx**
     - `jz01-45-v33` cho bo mạch **JZxxx**
     - `fy-mf800` cho bo mạch **MF800**


- Để cập nhật kernel cho image `debian`
- Lấy link kernel tại: http://mirror.postmarketos.org/postmarketos/master/aarch64/

  ```shell
  wget -O - https://mirror.postmarketos.org/postmarketos/master/aarch64/linux-postmarketos-qcom-msm8916-6.12.1-r6.apk \
          | tar xkzf - -C / --exclude=.PKGINFO --exclude=.SIGN* 2>/dev/null
  depmod -a 6.12.1-msm8916   # the package ships empty module indexes
  ```
