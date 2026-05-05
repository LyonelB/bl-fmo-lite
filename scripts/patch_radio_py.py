#!/usr/bin/env python3
"""
BL-FMO-LITE - scripts/patch_radio_py.py  v2
Patch Raspiaudio pour le RDS FM.
Usage : python3 patch_radio_py.py [--dir ~/Digital-Radio-for-Raspberry-Pi]
"""

import sys
import shutil
import argparse
from pathlib import Path
from datetime import datetime


def backup(path):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = path.parent / (path.name + ".bak_" + ts)
    shutil.copy2(path, bak)
    print("  Sauvegarde : " + bak.name)


def patch_text(path, old, new, label):
    raw = path.read_bytes()
    if b"\x00" in raw:
        print("  [ERR] Fichier corrompu - restaurez la sauvegarde")
        return False
    text = raw.decode("utf-8")
    if old not in text:
        print("  [SKIP] " + label)
        return False
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print("  [OK]  " + label)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dir",
        default=str(Path.home() / "Digital-Radio-for-Raspberry-Pi"),
    )
    args = parser.parse_args()
    root = Path(args.dir).expanduser().resolve()

    safe2   = root / "legacy" / "dab_radio_i2c_safe2.py"
    backend = root / "raspiaudio_radio" / "backend.py"
    server  = root / "raspiaudio_radio" / "server.py"
    decoder = root / "raspiaudio_radio" / "fm_rds_decoder.py"

    for p in (safe2, backend, server):
        if not p.exists():
            print("ERREUR : introuvable : " + str(p))
            sys.exit(1)

    # === Patch 1 : dab_radio_i2c_safe2.py ===
    print("\n=== Patch 1 : dab_radio_i2c_safe2.py ===")
    backup(safe2)

    patch_text(
        safe2,
        "CMD_FM_RSQ_STATUS = 0x32",
        "CMD_FM_RSQ_STATUS = 0x32\nCMD_FM_RDS_STATUS = 0x34",
        "Constante CMD_FM_RDS_STATUS",
    )

    method_lines = [
        "",
        "    def fm_rds_status(self, intack=False, mtfifo=False):",
        "        # Si468x FM_RDS_STATUS (0x34)",
        "        # reply[5]: flags (bit2=RDSSYNC)",
        "        # reply[6]: RDSFIFOUSED",
        "        # reply[7:9]:   Block A (PI, big-endian)",
        "        # reply[9:11]:  Block B",
        "        # reply[11:13]: Block C",
        "        # reply[13:15]: Block D",
        "        # reply[15]: BLERs (A=bits7:6, B=5:4, C=3:2, D=1:0)",
        "        flags = (0x01 if intack else 0x00) | (0x02 if mtfifo else 0x00)",
        "        self._write_command([CMD_FM_RDS_STATUS, flags])",
        "        reply = self._read_reply(16)",
        "        if len(reply) < 16:",
        "            return {'sync': False, 'fifo_used': 0}",
        "        return {",
        "            'sync':      bool(reply[5] & 0x04),",
        "            'fifo_used': reply[6],",
        "            'block_a':   (reply[7]  << 8) | reply[8],",
        "            'block_b':   (reply[9]  << 8) | reply[10],",
        "            'block_c':   (reply[11] << 8) | reply[12],",
        "            'block_d':   (reply[13] << 8) | reply[14],",
        "            'bler_a':    (reply[15] >> 6) & 0x03,",
        "            'bler_b':    (reply[15] >> 4) & 0x03,",
        "            'bler_c':    (reply[15] >> 2) & 0x03,",
        "            'bler_d':     reply[15]       & 0x03,",
        "        }",
        "",
        "    def am_tune(",
    ]
    patch_text(
        safe2,
        "    def am_tune(",
        "\n".join(method_lines),
        "Methode fm_rds_status()",
    )

    # === Patch 2 : fm_rds_decoder.py (nouveau fichier) ===
    print("\n=== Patch 2 : fm_rds_decoder.py ===")
    lines = [
        "# BL-FMO-LITE - fm_rds_decoder.py",
        "# Decodeur RDS FM : accumule les groupes et extrait PS, RT, PI.",
        "",
        "class FMRDSDecoder:",
        "",
        "    def __init__(self):",
        "        self.pi  = None",
        "        self.ps  = None",
        "        self.rt  = None",
        "        self.pty = None",
        "        self.tp  = None",
        "        self.ta  = None",
        "        self._ps_buf  = ['_'] * 8",
        "        self._ps_seen = [False] * 4",
        "        self._rt_buf  = [' '] * 64",
        "        self._rt_seen = set()",
        "        self._rt_flag = None",
        "",
        "    def update(self, g):",
        "        if not g.get('sync'):",
        "            return",
        "        a  = g['block_a']",
        "        b  = g['block_b']",
        "        c  = g['block_c']",
        "        d  = g['block_d']",
        "        ea = g['bler_a']",
        "        eb = g['bler_b']",
        "        ec = g['bler_c']",
        "        ed = g['bler_d']",
        "        if ea <= 2 and a:",
        "            self.pi = '{:04X}'.format(a)",
        "        if eb > 2:",
        "            return",
        "        gtype = (b >> 12) & 0x0F",
        "        b0    = (b >> 11) & 0x01",
        "        self.tp  = bool((b >> 10) & 0x01)",
        "        self.pty = (b >> 5) & 0x1F",
        "        if gtype == 0:",
        "            self.ta = bool((b >> 4) & 0x01)",
        "            seg = b & 0x03",
        "            if ed <= 2:",
        "                hi = d >> 8",
        "                lo = d & 0xFF",
        "                self._ps_buf[seg * 2]     = chr(hi) if hi >= 0x20 else '_'",
        "                self._ps_buf[seg * 2 + 1] = chr(lo) if lo >= 0x20 else '_'",
        "                self._ps_seen[seg] = True",
        "            if all(self._ps_seen):",
        "                self.ps = ''.join(self._ps_buf).strip()",
        "        elif gtype == 2 and b0 == 0:",
        "            flag = (b >> 4) & 0x01",
        "            if flag != self._rt_flag:",
        "                self._rt_buf  = [' '] * 64",
        "                self._rt_seen = set()",
        "                self._rt_flag = flag",
        "            seg = b & 0x0F",
        "            def ch(v):",
        "                return chr(v) if 0x20 <= v < 0x100 else ' '",
        "            if ec <= 2:",
        "                self._rt_buf[seg * 4]     = ch(c >> 8)",
        "                self._rt_buf[seg * 4 + 1] = ch(c & 0xFF)",
        "            if ed <= 2:",
        "                self._rt_buf[seg * 4 + 2] = ch(d >> 8)",
        "                self._rt_buf[seg * 4 + 3] = ch(d & 0xFF)",
        "            self._rt_seen.add(seg)",
        "            if self._rt_seen:",
        "                end = (max(self._rt_seen) + 1) * 4",
        "                raw = ''.join(self._rt_buf[:end]).split('\\r')[0]",
        "                self.rt = raw.strip() or None",
        "",
        "    def reset(self):",
        "        self.__init__()",
        "",
        "    def to_dict(self):",
        "        return {",
        "            'ps':  self.ps,",
        "            'pi':  self.pi,",
        "            'rt':  self.rt,",
        "            'pty': self.pty,",
        "            'tp':  self.tp,",
        "            'ta':  self.ta,",
        "        }",
    ]
    decoder.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("  [OK]  " + decoder.name)

    # === Patch 3 : backend.py ===
    print("\n=== Patch 3 : backend.py ===")
    backup(backend)

    patch_text(
        backend,
        "from legacy.dab_radio_i2c_safe2 import (",
        "from raspiaudio_radio.fm_rds_decoder import FMRDSDecoder\n"
        "from legacy.dab_radio_i2c_safe2 import (",
        "Import FMRDSDecoder",
    )

    patch_text(
        backend,
        "        self._last_signal: Optional[Dict[str, Any]] = None",
        "        self._last_signal: Optional[Dict[str, Any]] = None\n"
        "        self._fm_rds = FMRDSDecoder()",
        "Init self._fm_rds",
    )

    old_fm = (
        "        if station[\"band\"] == \"fm\":\n"
        "            return self._merge_fmhd_status"
        "(radio.fm_rsq_status(attune=True), radio.hd_digrad_status())"
    )
    new_fm = (
        "        if station[\"band\"] == \"fm\":\n"
        "            self._poll_fm_rds_locked(radio)\n"
        "            return self._merge_fmhd_status"
        "(radio.fm_rsq_status(attune=True), radio.hd_digrad_status())"
    )
    patch_text(backend, old_fm, new_fm, "Appel _poll_fm_rds_locked")

    patch_text(
        backend,
        "            \"signal\": dict(self._last_signal or {}),",
        "            \"signal\": dict(self._last_signal or {}),\n"
        "            \"rds\": self._fm_rds.to_dict(),",
        "RDS dans status payload",
    )

    poll_lines = [
        "    def _poll_fm_rds_locked(self, radio):",
        "        if not hasattr(radio, 'fm_rds_status'):",
        "            return",
        "        try:",
        "            for _ in range(8):",
        "                group = radio.fm_rds_status()",
        "                self._fm_rds.update(group)",
        "                if group.get('fifo_used', 0) <= 1:",
        "                    break",
        "        except Exception:",
        "            pass",
        "",
        "    def get_fm_rds(self):",
        "        with self._lock:",
        "            return self._fm_rds.to_dict()",
        "",
        "    def _recording_active_locked(self) -> bool:",
    ]
    patch_text(
        backend,
        "    def _recording_active_locked(self) -> bool:",
        "\n".join(poll_lines),
        "Methodes _poll_fm_rds_locked + get_fm_rds",
    )

    # === Patch 4 : server.py ===
    print("\n=== Patch 4 : server.py ===")
    backup(server)
    server_lines = [
        "        if parsed.path == \"/api/fm/rds\":",
        "            rds = self.server.backend.get_fm_rds()",
        "            self._send_ok({\"rds\": rds}, send_body=send_body)",
        "            return",
        "        if parsed.path == \"/api/live-metadata\":",
    ]
    patch_text(
        server,
        "        if parsed.path == \"/api/live-metadata\":",
        "\n".join(server_lines),
        "Endpoint /api/fm/rds",
    )

    print("\n=== Verification ===")
    for path, pattern in [
        (safe2,   "CMD_FM_RDS_STATUS"),
        (safe2,   "fm_rds_status"),
        (decoder, "FMRDSDecoder"),
        (backend, "FMRDSDecoder"),
        (backend, "_poll_fm_rds_locked"),
        (server,  "/api/fm/rds"),
    ]:
        found = pattern in path.read_text(encoding="utf-8")
        status = "[OK]" if found else "[MANQUANT]"
        print("  " + status + "  " + path.name + " : " + pattern)

    print("\nPatch termine. Redemarrer BL-FMO-LITE :")
    print("  pkill -f 'radio.py serve' && sleep 1 && python app.py")
    print("\nTest apres 30s :")
    print("  curl -s http://localhost:8686/api/fm/rds | python3 -m json.tool")


if __name__ == "__main__":
    main()
