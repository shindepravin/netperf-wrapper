## -*- coding: utf-8 -*-
##
## metadata.py
##
## Author:   Toke Høiland-Jørgensen (toke@toke.dk)
## Date:     27 January 2014
## Copyright (c) 2014, Toke Høiland-Jørgensen
##
## This program is free software: you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation, either version 3 of the License, or
## (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with this program.  If not, see <http://www.gnu.org/licenses/>.

import sys, os, socket, subprocess, time, re

from netperf_wrapper import util

class CommandRunner(object):

    def __init__(self):
        self.hostname = None
        self.env = os.environ.copy()
        self.fixup_path()

    def fixup_path(self):
        """Fix up the PATH to include /sbin and /usr/sbin since some of the
        utilities called (such as ip and tc) live here, and those directories
        are not normally in the path (on e.g. Debian)."""
        path = self.env['PATH'].split(':')
        if not '/sbin' in path:
            path.append('/sbin')
        if not '/usr/sbin' in path:
            path.append('/usr/sbin')
        self.env['PATH'] = ":".join(path)

    def set_hostname(self, hostname):
        self.hostname = hostname

    def __call__(self, command):
        """Try executing a command, and if successful,
        return the strip()'ed output, else None."""
        try:
            if self.hostname:
                command = "ssh %s '%s'" % (self.hostname, command)
            res = subprocess.check_output(command, universal_newlines=True, shell=True,
                                          stderr=subprocess.STDOUT, env=self.env)
            return res.strip()
        except subprocess.CalledProcessError:
            return None

get_command_output = CommandRunner()

__all__ = ['record_extended_metadata']

def record_extended_metadata(results, hostnames):
    m = results.meta()
    m['KERNEL_NAME'] = get_command_output("uname -s")
    m['KERNEL_RELEASE'] = get_command_output("uname -r")
    m['IP_ADDRS'] = get_ip_addrs()
    m['GATEWAYS'] = get_gateways()
    m['EGRESS_INFO'] = get_egress_info(target=m['HOST'], ip_version=m['IP_VERSION'])

    m['REMOTE_METADATA'] = {}

    for h in hostnames:
        get_command_output.set_hostname(h)
        m['REMOTE_METADATA'][h] = {}
        m['REMOTE_METADATA'][h]['LOCAL_HOST'] = get_command_output("hostname")
        m['REMOTE_METADATA'][h]['KERNEL_NAME'] = get_command_output("uname -s")
        m['REMOTE_METADATA'][h]['KERNEL_RELEASE'] = get_command_output("uname -r")
        m['REMOTE_METADATA'][h]['IP_ADDRS'] = get_ip_addrs()
        m['REMOTE_METADATA'][h]['GATEWAYS'] = get_gateways()
        m['REMOTE_METADATA'][h]['EGRESS_INFO'] = get_egress_info(target=m['HOST'], ip_version=m['IP_VERSION'])
        if 'src' in m['EGRESS_INFO']:
            m['REMOTE_METADATA'][h]['INGRESS_INFO'] = get_egress_info(target=m['EGRESS_INFO']['src'], ip_version=m['IP_VERSION'])
        m['REMOTE_METADATA'][h]['EGRESS_INFO'] = get_egress_info(target=m['HOST'], ip_version=m['IP_VERSION'])

def get_ip_addrs(iface=None):
    """Try to get IP addresses associated to this machine. Uses iproute2 if available,
    otherwise falls back to ifconfig."""
    addresses = {}

    cmd = "ip addr show"
    if iface is not None:
        cmd += " dev %s" % iface
    output = get_command_output(cmd)

    if output is None:
        cmd = "ifconfig"
        if iface is not None:
            cmd += " %s" % iface
        output = get_command_output(cmd)

    iface_re = re.compile('^([0-9]+: )?([a-z0-9-]+):')

    if output is not None:
        lines = output.splitlines()
        iface = None
        addrs = []
        for l in lines:
            # Both ifconfig and iproute2 emit addresses on lines starting with the address
            # identifier, and fields are whitespace-separated. Look for that and parse
            # accordingly.
            m = iface_re.match(l)
            if m is not None:
                if iface and addrs:
                    addresses[iface] = addrs
                iface = m.group(2)
                addrs = []
            parts = l.strip().split()
            if parts and parts[0] in ('inet', 'inet6'):
                a =  parts[1]
                if '/' in a: # iproute2 adds subnet qualification; strip that out
                    a = a[:a.index('/')]
                if '%' in a: # BSD may add interface qualification; strip that out
                    a = a[:a.index('%')]
                addrs.append(a)
        if addrs and iface:
            addresses[iface] = addrs
    return addresses or None

def get_offloads(iface):
    offloads = {}

    output = get_command_output("ethtool -k %s" % iface)
    val_map = {'on': True, 'off': False}
    interesting_offloads = ['tcp-segmentation-offload',
                            'udp-fragmentation-offload',
                            'large-receive-offload',
                            'generic-segmentation-offload',
                            'generic-receive-offload']
    if output is not None:
        for l in output.splitlines():
            parts = l.split()
            key = parts[0].strip(":")
            if key in interesting_offloads:
                try:
                    offloads[key] = val_map[parts[1]]
                except KeyError:
                    continue
    return offloads or None


def get_gateways():
    gws = []
    # Linux netstat only outputs IPv4 data by default, but can be made to output both
    # if passed both -4 and -6
    output = get_command_output("netstat -46nr")
    if output is None:
        # If that didn't work, maybe netstat doesn't support -4/-6 (e.g. BSD), so try
        # without
        output = get_command_output("netstat -nr")
    if output is not None:
        output = output.replace("Next Hop", "Next_Hop") # breaks part splitting
        lines = output.splitlines()
        iface_idx = None

        for line in lines:
            parts = line.split()
            if not parts:
                continue

            # Try to find the column header; should have "Destination" as first word.
            if parts[0] == "Destination":
                # Linux uses Iface or If as header (for IPv4/6), FreeBSD uses If
                for n in ("Iface", "Netif", "If"):
                    if n in parts:
                        iface_idx = parts.index(n)
            if parts[0] in ("0.0.0.0", "default", "::/0"):
                if iface_idx is not None:
                    # The fields may run into each other in some instances; try
                    # to detect this, and if so just assume that the interface
                    # name is the last field (it often is, on Linux).
                    if iface_idx > len(parts)-1:
                        iface_idx = -1
                    gw = {'ip': parts[1], 'iface': parts[iface_idx]}
                    if not gw['iface'].startswith('lo'):
                        gws.append(gw)
                else:
                    gws.append({'ip': parts[1]})
    return gws

def get_egress_info(target, ip_version):
    route = {}

    if target:
        ip = util.lookup_host(target, ip_version)[4][0]
        output = get_command_output("ip route get %s" % ip)
        if output is not None:
            # Linux iproute2 syntax. Example:
            # $ ip r get 8.8.8.8
            # 8.8.8.8 via 10.109.3.254 dev wlan0  src 10.109.0.146
            #     cache
            parts = iter(output.split())
            for p in parts:
                if p == 'via':
                    route['nexthop'] = next(parts)
                elif p == 'dev':
                    route['iface'] = next(parts)
                elif p == 'src':
                    route['src'] = next(parts)
        else:
            output = get_command_output("route -n get %s" % ip)
            if output is not None:
                # BSD syntax. Example:
                # $ route -n get 8.8.8.8
                #    route to: 8.8.8.8
                # destination: default
                #        mask: default
                #     gateway: 10.42.7.225
                #   interface: em0
                #       flags: <UP,GATEWAY,DONE,STATIC>
                #  recvpipe  sendpipe  ssthresh  rtt,msec    mtu        weight    expire
                #        0         0         0         0      1500         1         0

                for line in output.splitlines():
                    if not ":" in line:
                        continue
                    k,v = [i.strip() for i in line.split(":")]
                    if k == "gateway":
                        route['nexthop'] = v
                    if k == "interface":
                        route['iface'] = v

    if route:
        route['qdiscs'] = get_qdiscs(route['iface'])
        route['classes'] = get_classes(route['iface'])
        route['offloads'] = get_offloads(route['iface'])
        route['bql'] = get_bql(route['iface'])
        route['driver'] = get_driver(route['iface'])
        route['target'] = ip
        if not 'nexthop' in route:
            route['nexthop'] = None

    return route or None

def parse_tc(cmd, kind):
    items = []

    output = get_command_output(cmd)
    if output is not None:
        lines = output.splitlines()
        for line in lines:
            parts = line.split()
            if not parts or parts[0] != kind:
                continue
            item = {'name': parts[1],
                    'id': parts[2]}
            if parts[3] == 'root':
                item['parent'] = 'root'
                params = parts[4:]
            else:
                item['parent'] = parts[4]
                params = parts[5:]

            # Assume that the remainder of the output line is a set of space delimited
            # key/value pairs. Some qdiscs (e.g. fq_codel) has a single non-valued parameter
            # at the end, in which case the length of params will be uneven. In this case an
            # empty string is added as the parameter "value", to make sure it is included.
            if len(params) % 2 > 0:
                params.append("")
            item['params'] = dict(zip(params[::2], params[1::2]))

            items.append(item)
    return items or None

def get_qdiscs(iface):
    return parse_tc("tc qdisc show dev %s" % iface, "qdisc")

def get_classes(iface):
    return parse_tc("tc class show dev %s" % iface, "class")

def get_bql(iface):
    bql = []
    output = get_command_output('for i in /sys/class/net/%s/queues/tx-*; do [ -d $i/byte_queue_limits ] && echo -n "$(basename $i) " && cat $i/byte_queue_limits/limit_max; done' % iface)
    if output is not None:
        bql = dict([i.split() for i in output.splitlines()])

    return bql or None

def get_driver(iface):
    return get_command_output("basename $(readlink /sys/class/net/%s/device/driver)" % iface)
