#!/usr/bin/env python3
"""Query performance-relevant ACL device attributes, never identities or credentials."""
import argparse
import ctypes as c
import hashlib
import json
from pathlib import Path


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    lib=c.CDLL('libascendcl.so')
    lib.aclInit.argtypes=[c.c_char_p];lib.aclInit.restype=c.c_int
    lib.aclrtGetDeviceCount.argtypes=[c.POINTER(c.c_uint32)];lib.aclrtGetDeviceCount.restype=c.c_int
    lib.aclrtGetDeviceInfo.argtypes=[c.c_uint32,c.c_int,c.POINTER(c.c_int64)]
    lib.aclrtGetDeviceInfo.restype=c.c_int
    lib.aclFinalize.argtypes=[];lib.aclFinalize.restype=c.c_int
    report={'scope':'ACL runtime device attributes; no SetDevice, allocation or kernel',
            'header':'CANN 9.0.0 include/acl/acl_rt.h aclrtDevAttr enum',
            'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    spec=Path('/home/developer/Ascend/cann-9.0.0/aarch64-linux/data/platform_config/Ascend910B3.ini')
    if spec.exists():
        wanted={'ai_core_cnt','cube_core_cnt','vector_core_cnt','l2_size','ub_size','l1_size','cube_vector_combine'}
        values={}
        for line in spec.read_text().splitlines():
            key,sep,value=line.partition('=')
            if sep and key.strip() in wanted: values[key.strip()]=value.strip()
        report['installed_sdk_spec']={'path':str(spec),'values':values,'scope':'SDK configuration, not a capacity measurement'}
    report['notice']='Runtime UBUF attribute may return zero on this platform; do not interpret that as physical UB capacity.'
    rc=lib.aclInit(None);report['aclInit_returncode']=rc
    if rc==0:
        try:
            count=c.c_uint32();rc=lib.aclrtGetDeviceCount(c.byref(count))
            report['visible_devices']=count.value
            if rc or count.value!=1: raise RuntimeError('Expected exactly one assigned runtime device')
            report['attributes']={}
            for name,attr in [('aicpu_cores',1),('aicore_cores',101),('cube_cores',102),
                              ('vector_cores',201),('ubuf_bytes_per_vector',204),
                              ('global_mem_bytes',301),('l2_cache_bytes',302),('is_virtual',501)]:
                value=c.c_int64();rc=lib.aclrtGetDeviceInfo(0,attr,c.byref(value))
                report['attributes'][name]={'enum':attr,'returncode':rc,'value':value.value if rc==0 else None}
        finally:
            report['aclFinalize_returncode']=lib.aclFinalize()
    with args.out.open('x') as f: json.dump(report,f,indent=2);f.write('\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
