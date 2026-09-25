// Compare private input copies with true cross-block sharing of the same GM addresses.
#include "kernel_operator.h"
using namespace AscendC;

extern "C" __global__ __aicore__ void sharing_probe(
    GM_ADDR x, GM_ADDR y, GM_ADDR z, uint32_t length,
    uint32_t tile, uint32_t rounds, uint32_t shared) {
    TPipe pipe;
    TQue<QuePosition::VECIN,2> qx,qy;
    TQue<QuePosition::VECOUT,2> qz;
    GlobalTensor<float> gx,gy,gz;
    uint32_t base=GetBlockIdx()*length;
    gx.SetGlobalBuffer((__gm__ float*)x+base,length);
    gy.SetGlobalBuffer((__gm__ float*)y+(shared?0:base),length);
    gz.SetGlobalBuffer((__gm__ float*)z+base,length);
    pipe.InitBuffer(qx,2,tile*4);
    pipe.InitBuffer(qy,2,tile*4);
    pipe.InitBuffer(qz,2,tile*4);
    for(uint32_t offset=0;offset<length;offset+=tile) {
        auto a=qx.AllocTensor<float>(), b=qy.AllocTensor<float>();
        DataCopy(a,gx[offset],tile);DataCopy(b,gy[offset],tile);
        qx.EnQue(a);qy.EnQue(b);
        a=qx.DeQue<float>();b=qy.DeQue<float>();
        auto c=qz.AllocTensor<float>();
        Add(c,a,b,tile);
        for(uint32_t r=1;r<rounds;r++) { PipeBarrier<PIPE_V>();Add(c,c,b,tile); }
        qz.EnQue(c);qx.FreeTensor(a);qy.FreeTensor(b);
        c=qz.DeQue<float>();DataCopy(gz[offset],c,tile);qz.FreeTensor(c);
    }
}
