// Mechanism probe: identical x + rounds*y arithmetic, configurable tiling/queues.
// Uses documented Ascend C queues and vector APIs; no framework fusion.
#include "kernel_operator.h"
using namespace AscendC;

template<int DEPTH> class VectorProbe {
public:
    __aicore__ inline void Run(GM_ADDR x, GM_ADDR y, GM_ADDR z,
                              uint32_t length, uint32_t tile, uint32_t rounds) {
        uint32_t base = GetBlockIdx() * length;
        xGm.SetGlobalBuffer((__gm__ float*)x + base, length);
        yGm.SetGlobalBuffer((__gm__ float*)y + base, length);
        zGm.SetGlobalBuffer((__gm__ float*)z + base, length);
        pipe.InitBuffer(qx, DEPTH, tile * sizeof(float));
        pipe.InitBuffer(qy, DEPTH, tile * sizeof(float));
        pipe.InitBuffer(qz, DEPTH, tile * sizeof(float));
        for (uint32_t offset = 0; offset < length; offset += tile) {
            auto a = qx.template AllocTensor<float>();
            auto b = qy.template AllocTensor<float>();
            DataCopy(a, xGm[offset], tile);
            DataCopy(b, yGm[offset], tile);
            qx.EnQue(a);
            qy.EnQue(b);
            a = qx.template DeQue<float>();
            b = qy.template DeQue<float>();
            auto c = qz.template AllocTensor<float>();
            Add(c, a, b, tile);
            for (uint32_t r = 1; r < rounds; ++r) {
                PipeBarrier<PIPE_V>();
                Add(c, c, b, tile);
            }
            qz.EnQue(c);
            qx.FreeTensor(a);
            qy.FreeTensor(b);
            c = qz.template DeQue<float>();
            DataCopy(zGm[offset], c, tile);
            qz.FreeTensor(c);
        }
    }
private:
    TPipe pipe;
    TQue<QuePosition::VECIN, DEPTH> qx, qy;
    TQue<QuePosition::VECOUT, DEPTH> qz;
    GlobalTensor<float> xGm, yGm, zGm;
};

extern "C" __global__ __aicore__ void vector_probe(
    GM_ADDR x, GM_ADDR y, GM_ADDR z, uint32_t length,
    uint32_t tile, uint32_t rounds, uint32_t buffers) {
    if (buffers == 1) {
        VectorProbe<1> op;
        op.Run(x, y, z, length, tile, rounds);
    } else {
        VectorProbe<2> op;
        op.Run(x, y, z, length, tile, rounds);
    }
}
