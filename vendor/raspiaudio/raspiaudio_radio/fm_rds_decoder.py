# BL-FMO-LITE - fm_rds_decoder.py
# Decodeur RDS FM : accumule les groupes et extrait PS, RT, PI.

class FMRDSDecoder:

    def __init__(self):
        self.pi  = None
        self.ps  = None
        self.rt  = None
        self.pty = None
        self.tp  = None
        self.ta  = None
        self._ps_buf  = ['_'] * 8
        self._ps_seen = [False] * 4
        self._rt_buf  = [' '] * 64
        self._rt_seen = set()
        self._rt_flag = None

    def update(self, g):
        if not g.get('sync'):
            return
        a  = g['block_a']
        b  = g['block_b']
        c  = g['block_c']
        d  = g['block_d']
        ea = g['bler_a']
        eb = g['bler_b']
        ec = g['bler_c']
        ed = g['bler_d']
        if ea <= 2 and a:
            self.pi = '{:04X}'.format(a)
        if eb > 2:
            return
        gtype = (b >> 12) & 0x0F
        b0    = (b >> 11) & 0x01
        self.tp  = bool((b >> 10) & 0x01)
        self.pty = (b >> 5) & 0x1F
        if gtype == 0:
            self.ta = bool((b >> 4) & 0x01)
            seg = b & 0x03
            if ed <= 2:
                hi = d >> 8
                lo = d & 0xFF
                self._ps_buf[seg * 2]     = chr(hi) if hi >= 0x20 else '_'
                self._ps_buf[seg * 2 + 1] = chr(lo) if lo >= 0x20 else '_'
                self._ps_seen[seg] = True
            if all(self._ps_seen):
                self.ps = ''.join(self._ps_buf).strip()
        elif gtype == 2 and b0 == 0:
            flag = (b >> 4) & 0x01
            if flag != self._rt_flag:
                self._rt_buf  = [' '] * 64
                self._rt_seen = set()
                self._rt_flag = flag
            seg = b & 0x0F
            def ch(v):
                return chr(v) if 0x20 <= v < 0x100 else ' '
            if ec <= 2:
                self._rt_buf[seg * 4]     = ch(c >> 8)
                self._rt_buf[seg * 4 + 1] = ch(c & 0xFF)
            if ed <= 2:
                self._rt_buf[seg * 4 + 2] = ch(d >> 8)
                self._rt_buf[seg * 4 + 3] = ch(d & 0xFF)
            self._rt_seen.add(seg)
            if self._rt_seen:
                end = (max(self._rt_seen) + 1) * 4
                raw = ''.join(self._rt_buf[:end]).split('\r')[0]
                self.rt = raw.strip() or None

    def reset(self):
        self.__init__()

    def to_dict(self):
        return {
            'ps':  self.ps,
            'pi':  self.pi,
            'rt':  self.rt,
            'pty': self.pty,
            'tp':  self.tp,
            'ta':  self.ta,
        }
