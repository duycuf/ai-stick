# AI Stick

Debian on a $10 4G USB dongle (Qualcomm MSM8916), built to host a personal AI assistant that you talk to over Telegram.

> [!WARNING]
> **Work in progress.** This repo currently only builds the firmware image. The image has not been flashed and tested from this repo yet. Flashing can brick the dongle: back up the whole eMMC first.

## Target hardware

| | |
|---|---|
| Board | `JZ01-45-V33` (silkscreen `JZ0145_V33`, sticker `JZ01-45-@`), sold as "4G-UFI" USB wifi dongle |
| SoC | Qualcomm MSM8916, HWID `0x007050e1` |
| Storage | 4 GB eMMC (7,733,248 sectors) |
| Stock firmware | Android 4.4.4, web UI `UFI-JZ_V3.0.0` at `192.168.100.1` |

Other boards supported by the builder: `uz801`, `ufi001c` (untested here).

## Build

Run the **Build** workflow from the Actions tab and pick the board (default `jz01-45-v33`). The firmware files are attached to the run as an artifact.

Locally, on Ubuntu:

```shell
git clone --recurse-submodules https://github.com/duycuf/ai-stick.git
cd ai-stick
sudo BOARD=jz01-45-v33 ./build.sh
```

## Credits

This repo is not a GitHub fork, but it is built on other people's work under the MIT license (see [LICENSE](LICENSE)):

- [kinsamanka/OpenStick-Builder](https://github.com/kinsamanka/OpenStick-Builder): the original image builder.
- [Mio-sha512](https://github.com/Mio-sha512/OpenStick-Builder) and [dbrant](https://github.com/dbrant/OpenStick-Builder): hotspot, tor, nftables and hostapd setup.
- [lastbest/OpenStick-Builder](https://github.com/lastbest/OpenStick-Builder): the `BOARD=` option, the lk2nd patch and every fix for the JZ01-45-V33 board, the admin page, and [docs/UF902_JZ01_NOTES.md](docs/UF902_JZ01_NOTES.md). The source tree here starts from its `main` branch at commit `1b2b765`.
- [msm8916-mainline](https://github.com/msm8916-mainline) (lk2nd, qhypstub, qtestsign), [postmarketOS](https://postmarketos.org/) (kernel) and the [OpenStick](https://github.com/OpenStick/OpenStick) project.

---

# Builder documentation

The rest of this file is the builder's own documentation, kept from upstream. Clone URLs and branch names in it refer to the upstream repositories.

This builder uses the precompiled [kernel](https://pkgs.postmarketos.org/package/master/postmarketos/aarch64/linux-postmarketos-qcom-msm8916) provided by [postmarketOS](https://postmarketos.org/) for Qualcomm MSM8916 devices.

## Build Instructions
### Build locally
This has been tested to work on **Ubuntu 22.04, 24.04 and 25.04**
- clone
  ```shell
  git clone --recurse-submodules https://github.com/Mio-sha512/OpenStick-Builder.git
  cd OpenStick-Builder/
  ```
#### Quick (Debian branch)
```shell
git checkout debian
sudo ./build.sh
```
#### Quick (Ubuntu 24.04 branch)
```shell
git checkout ubuntu-24.04
sudo ./build.sh
```
#### Selecting the board
lk1st (the lk2nd build used as `aboot`) cannot autodetect these dongles, so the
board decides which button GPIO lk2nd watches and which kernel devicetree is put
in `extlinux.conf`. Pass `BOARD=` to the build (default `uz801`):

| `BOARD=` | Boards | lk2nd compatible | kernel DTB |
| --- | --- | --- | --- |
| `uz801` (default) | YiMing UZ801 v3 | `yiming,uz801-v3` | `msm8916-yiming-uz801v3` |
| `jz01-45-v33` | JZ01-45-V33 "JZ" UFI dongles (e.g. UF902) | `openstick,jz01-45-v33` | `msm8916-jz01-45-v33` |
| `ufi001c` | UFI-001B/C | `thwc,ufi001c` | `msm8916-thwc-ufi001c` |

```shell
sudo BOARD=jz01-45-v33 ./build.sh
```
> [!IMPORTANT]
> Building a non-UZ801 board with the default `uz801` lk2nd makes lk2nd read the
> wrong GPIO as a held button. On JZ01-45-V33 this sends every boot to
> recovery/fastboot (`ERROR: No recovery partition found`), so Linux never starts.

See [docs/UF902_JZ01_NOTES.md](docs/UF902_JZ01_NOTES.md) for the full UF902 / JZ01-45-V33 write-up.

#### Detailed (same for any branch)
- install dependencies
  ```shell
  sudo scripts/install_deps.sh
  ```
- build hyp and lk2nd

  These custom bootloaders allow basic support for an `extlinux.conf` file, similar to U-Boot and Depthcharge.
  ```shell
  sudo scripts/build_hyp_aboot.sh
  ```
- extract Qualcomm firmware

  Extracts the bootloader and creates a new partition table that utilizes the full eMMC space
  ```shell
  sudo scripts/extract_fw.sh
  ```
- create rootfs using debootstrap
  ```shell
  sudo scripts/debootstrap.sh
  ```

- build gadget-tools
  ```shell
  sudo scripts/build_gt.sh
  ```
- create images
  ```shell
  sudo scripts/build_images.sh
  ```

The generated firmware files will be stored under the `files` directory

### On the cloud using Github Actions
1. Fork this repo
2. Run the [Build workflow](../../actions/workflows/build.yml)
   - click and run ***Run workflow***
   - once the workflow is done, click on the workflow summary and then download the resulting artifact

## Admin page
Images include a small web admin page on **http://192.168.100.1/** (LTE status, WiFi,
APN, clients, reboot …), see [`admin/`](admin/README.md). Build with `WITH_ADMIN=0` to
leave it out, or run `admin/install.sh` on an already flashed stick.

## Customizations
Edit [`scripts/setup.sh`](scripts/setup.sh) to add/remove packages. Note that this script is running inside the `chroot` environment.

## Firmware Installation
> [!WARNING]  
> The following commands can potentially brick your device, making it unbootable. Proceed with caution and at your own risk!

> [!IMPORTANT]  
> Make sure to perform a backup of the original firmware using the command `edl rf orig_fw.bin`

### Prerequisites
- [EDL](https://github.com/bkerler/edl)
- Android fastboot tool
  ```
  sudo apt install fastboot
  ```

### Steps
- Ensure that your device is running the stock firmware
- Enter Qualcomm EDL mode using this [guide](https://wiki.postmarketos.org/wiki/Zhihe_series_LTE_dongles_(generic-zhihe)#How_to_enter_flash_mode)
- Backup required partitions

  The following files are required from the original firmware:

     - `fsc.bin`
     - `fsg.bin`
     - `modem.bin`
     - `modemst1.bin`
     - `modemst2.bin`
     - `persist.bin`
     - `sec.bin`

  Skip this step if these files are already present
  ```shell
  for n in fsc fsg modem modemst1 modemst2 persist sec; do
      edl r ${n} ${n}.bin
  done
  ```
- Install `aboot`
  ```shell
  edl w aboot aboot.mbn
  ```
- Reboot to fastboot
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
- Restore original partitions
  ```shell
  for n in fsc fsg modem modemst1 modemst2 persist sec; do
      fastboot flash ${n} ${n}.bin
  done
  ```
- Reboot
  ```shell
  fastboot reboot
  ```

## Post-Install
- Network configuration
  
  |  | |
  | ----- | ---- |
  | ssid | 4G-UFI-XX |
  | password | 1234567890 |
  | ip addr | 192.168.100.1 |

- Default user
  
  | | |
  | ----- | ---- |
  | username | user |
  | password | 1 |
 
- If you built with the default `BOARD=uz801` but your device is not a **UZ801**, modify `/boot/extlinux/extlinux.conf` to use the correct devicetree (better: rebuild with the right `BOARD=`, see above)
  ```shell
  sed -i 's/yiming-uz801v3/<BOARD>/' /boot/extlinux/extlinux.conf
  ```

  where `<BOARD>` is
     - `thwc-uf896` for **UF896** boards
     - `thwc-ufi001c` for **UFIxxx** boards
     - `jz01-45-v33` for **JZxxx** boards
     - `fy-mf800` for **MF800** boards


- To update the kernel of the `debian` image
- Get the kernel link from: http://mirror.postmarketos.org/postmarketos/master/aarch64/

  ```shell
  wget -O - https://mirror.postmarketos.org/postmarketos/master/aarch64/linux-postmarketos-qcom-msm8916-6.12.1-r6.apk \
          | tar xkzf - -C / --exclude=.PKGINFO --exclude=.SIGN* 2>/dev/null
  depmod -a 6.12.1-msm8916   # the package ships empty module indexes
  ```
