import math
import socket
import sys
import multiprocessing as mp
from typing import Callable, Optional, Tuple, Any

from construct import ConstError, Struct


class EventPipe:
    def __init__(self, duplex=False):
        self.event = mp.Event()
        # TODO: Any value in duplex?
        # If not duplex, can only transfer data from parent to child
        self.child_pipe, self.parent_pipe = mp.Pipe(duplex=duplex)

        # function aliases
        self.set = self.event.set
        self.is_set = self.event.is_set
        self.clear = self.event.clear
        # self.child_send = self.child_pipe.send
        self.child_poll = self.child_pipe.poll
        # self.parent_recv = self.parent_pipe.recv
        # self.parent_poll = self.parent_pipe.poll

    def parent_send(self, *args, **kwargs):
        if not self.is_set():
            # Only send when data has been received
            self.parent_pipe.send(*args, **kwargs)
            self.set()

    def child_recv(self, *args, **kwargs) -> Any:
        msg = self.child_pipe.recv(*args, **kwargs)
        self.clear()
        return msg


def offset_fg_radian(in_rad: float) -> float:
    """
    Even when echoing back literally what FG sends over the Net FDM connection,
    (i.e. UDP bytes in -> UDP bytes out) the latitude/longitude shown in FG
    appear to decrease. After plotting the offsets at a couple different lat/lons
    it appears to be a linear relationship and identical in anything that is
    represented in radians. The coefficient was chosen through trial-and-error.
    :param in_rad: Input property, in radians
    :return: Offset that needs to be applied to the input, in radians
    """
    coeff = 1.09349403e-9
    return math.degrees(in_rad) * coeff


def fix_fg_radian_parsing(s: Struct) -> Struct:
    s.lon_rad += offset_fg_radian(s.lon_rad)
    s.lat_rad += offset_fg_radian(s.lat_rad)
    s.phi_rad += offset_fg_radian(s.phi_rad)
    s.theta_rad += offset_fg_radian(s.theta_rad)
    s.psi_rad += offset_fg_radian(s.psi_rad)
    s.alpha_rad += offset_fg_radian(s.alpha_rad)
    s.beta_rad += offset_fg_radian(s.beta_rad)
    s.phidot_rad_per_s += offset_fg_radian(s.phidot_rad_per_s)
    s.thetadot_rad_per_s += offset_fg_radian(s.thetadot_rad_per_s)
    s.psidot_rad_per_s += offset_fg_radian(s.psidot_rad_per_s)
    return s


rx_callback_type = Callable[[Struct, EventPipe], Struct]


class FGConnection:
    fg_net_struct: Optional[Struct] = None

    def __init__(self):
        self.event_pipe = EventPipe(duplex=False)

        self.fg_rx_sock: Optional[socket.socket] = None
        self.fg_rx_cb: Optional[rx_callback_type] = None

        self.fg_tx_sock: Optional[socket.socket] = None
        self.fg_tx_addr: Optional[Tuple[str, int]] = None

        self.rx_proc: Optional[mp.Process] = None

    def connect_rx(self, fg_host: str, fg_port: int, rx_cb: rx_callback_type) -> EventPipe:
        # TODO: Support TCP server so that we only need 1 port
        self.fg_rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        fg_rx_addr = (fg_host, fg_port)
        self.fg_rx_sock.bind(fg_rx_addr)
        self.fg_rx_cb = rx_cb

        return self.event_pipe

    def connect_tx(self, fg_host: str, fg_port: int):
        self.fg_tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.fg_tx_addr = (fg_host, fg_port)

    def _rx_process(self):
        while True:
            # Receive up to 1KB of data from FG
            # blocking is fine here since we're in a separate process
            rx_msg, _ = self.fg_rx_sock.recvfrom(1024)
            try:
                s = self.fg_net_struct.parse(rx_msg)
            except ConstError as e:
                raise AssertionError(f'Could not decode FG stream. Is this the right FDM version?\n{e}')

            # Fix FG's radian parsing error :(
            s = fix_fg_radian_parsing(s)

            # Call user method
            if self.event_pipe.is_set() and self.event_pipe.child_poll():
                # only update when we have data to send
                s = self.fg_rx_cb(s, self.event_pipe)
            else:
                print('Receiving FG updates but no data to send', flush=True)
            sys.stdout.flush()
            tx_msg = self.fg_net_struct.build(dict(**s))
            # Send data back to FG
            if self.fg_tx_sock is not None:
                self.fg_tx_sock.sendto(tx_msg, self.fg_tx_addr)
            else:
                print(f'Warning: TX not connected, not sending updates to FG for RX {self.fg_rx_sock.getsockname()}')

    def start(self):
        self.rx_proc = mp.Process(target=self._rx_process)
        self.rx_proc.start()

    def stop(self):
        self.rx_proc.terminate()


class FDMConnection(FGConnection):
    def __init__(self, fdm_version: int):
        super().__init__()
        # TODO: Support auto-version check
        if fdm_version == 24:
            from .fdm_v24 import fdm_struct
        elif fdm_version == 25:
            from .fdm_v25 import fdm_struct
        else:
            raise NotImplementedError(f'FDM version {fdm_version} not supported yet')
        self.fg_net_struct = fdm_struct
