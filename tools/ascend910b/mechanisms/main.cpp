#include "acl/acl.h"
#include "aclrtlaunch_vector_probe.h"
#include "aclrtlaunch_sharing_probe.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#define ACL(call) do { auto rc = (call); if(rc != ACL_SUCCESS) { \
    throw std::runtime_error(std::string(#call) + " => " + std::to_string(rc)); }} while(0)

struct Config {
    std::string id, mode;
    uint32_t n, blocks, tile, buffers, rounds, groups, repeats;
};

class Bench {
    float *x=nullptr, *y=nullptr, *z=nullptr;
    size_t capacity;
    aclrtStream control;
    std::vector<aclrtStream> workers;
    std::vector<aclrtEvent> mid, done;
    aclrtEvent start, stop;
public:
    Bench(size_t elements):capacity(elements) {
        ACL(aclInit(nullptr));
        uint32_t count=0; ACL(aclrtGetDeviceCount(&count));
        if(count!=1) throw std::runtime_error("Expected one assigned logical device");
        ACL(aclrtSetDevice(0));
        ACL(aclrtCreateStream(&control));
        ACL(aclrtCreateEvent(&start)); ACL(aclrtCreateEvent(&stop));
        for(int i=0;i<8;i++) {
            aclrtStream s; aclrtEvent a,b;
            ACL(aclrtCreateStream(&s)); ACL(aclrtCreateEvent(&a)); ACL(aclrtCreateEvent(&b));
            workers.push_back(s); mid.push_back(a); done.push_back(b);
        }
        ACL(aclrtMalloc((void**)&x,capacity*4,ACL_MEM_MALLOC_HUGE_FIRST));
        ACL(aclrtMalloc((void**)&y,capacity*4,ACL_MEM_MALLOC_HUGE_FIRST));
        ACL(aclrtMalloc((void**)&z,capacity*4,ACL_MEM_MALLOC_HUGE_FIRST));
        const size_t chunk=1<<20;
        std::vector<float> h(chunk);
        for(size_t off=0;off<capacity;off+=chunk) {
            size_t m=std::min(chunk,capacity-off);
            for(size_t j=0;j<m;j++) h[j]=float(int((off+j)%251)-125)*0.25f;
            ACL(aclrtMemcpy(x+off,m*4,h.data(),m*4,ACL_MEMCPY_HOST_TO_DEVICE));
            for(size_t j=0;j<m;j++) h[j]=float(int((off+j)%31)-15)*0.125f;
            ACL(aclrtMemcpy(y+off,m*4,h.data(),m*4,ACL_MEMCPY_HOST_TO_DEVICE));
        }
    }
    ~Bench() {
        aclrtSynchronizeDevice();
        aclrtFree(x); aclrtFree(y); aclrtFree(z);
        for(auto s:workers) aclrtDestroyStream(s);
        for(auto e:mid) aclrtDestroyEvent(e);
        for(auto e:done) aclrtDestroyEvent(e);
        aclrtDestroyEvent(start); aclrtDestroyEvent(stop);
        aclrtDestroyStream(control); aclrtResetDevice(0); aclFinalize();
    }
    void launch(const Config& c, aclrtStream s, size_t off, uint32_t rounds, bool inplace=false) {
        if(c.mode=="shared" || c.mode=="private") {
            ACL(ACLRT_LAUNCH_KERNEL(sharing_probe)(c.blocks,s,x+off,y+off,z+off,
                c.n/c.blocks,c.tile,rounds,uint32_t(c.mode=="shared")));
            return;
        }
        ACL(ACLRT_LAUNCH_KERNEL(vector_probe)(c.blocks,s,
            inplace?z+off:x+off,y+off,z+off,c.n/c.blocks,c.tile,rounds,c.buffers));
    }
    void sequence(const Config& c) {
        if(c.mode=="barrier" || c.mode=="branch") {
            for(uint32_t g=0;g<c.groups;g++) {
                ACL(aclrtStreamWaitEvent(workers[g],start));
                launch(c,workers[g],size_t(g)*c.n,g%2?1:c.rounds);
                ACL(aclrtRecordEvent(mid[g],workers[g]));
            }
            for(uint32_t g=0;g<c.groups;g++) {
                if(c.mode=="barrier")
                    for(uint32_t j=0;j<c.groups;j++) ACL(aclrtStreamWaitEvent(workers[g],mid[j]));
                launch(c,workers[g],size_t(g)*c.n,g%2?c.rounds:1,true);
                ACL(aclrtRecordEvent(done[g],workers[g]));
            }
            for(uint32_t g=0;g<c.groups;g++) ACL(aclrtStreamWaitEvent(control,done[g]));
        } else if(c.mode=="cache_adjacent") {
            for(uint32_t g=0;g<c.groups;g++)
                for(uint32_t r=0;r<c.repeats;r++) launch(c,control,size_t(g)*c.n,c.rounds);
        } else if(c.mode=="cache_roundrobin") {
            for(uint32_t r=0;r<c.repeats;r++)
                for(uint32_t g=0;g<c.groups;g++) launch(c,control,size_t(g)*c.n,c.rounds);
        } else if(c.mode=="materialize") {
            for(uint32_t r=0;r<c.repeats;r++) launch(c,control,0,c.rounds,r!=0);
        } else if(c.mode=="fused") {
            launch(c,control,0,c.rounds*c.repeats);
        } else if(c.mode=="pipe" || c.mode=="shared" || c.mode=="private") {
            for(uint32_t r=0;r<c.repeats;r++) launch(c,control,0,c.rounds);
        } else throw std::runtime_error("Unknown mode: "+c.mode);
    }
    std::pair<double,double> run(const Config& c) {
        auto t=std::chrono::steady_clock::now();
        ACL(aclrtRecordEvent(start,control)); sequence(c);
        ACL(aclrtRecordEvent(stop,control)); ACL(aclrtSynchronizeStream(control));
        float ms=0; ACL(aclrtEventElapsedTime(&ms,start,stop));
        double host=std::chrono::duration<double,std::micro>(std::chrono::steady_clock::now()-t).count();
        return {ms*1000.,host};
    }
    double verify(const Config& c) {
        uint32_t groups=(c.mode=="barrier"||c.mode=="branch"||c.mode.find("cache_")==0)?c.groups:1;
        uint32_t adds=c.rounds;
        if(c.mode=="materialize"||c.mode=="fused") adds*=c.repeats;
        if(c.mode=="barrier"||c.mode=="branch") adds++;
        std::vector<float> h(1<<20), expected(1<<20), period(251*31); double error=0;
        for(size_t j=0;j<period.size();j++) {
            float a=float(int(j%251)-125)*0.25f;
            float b=float(int(j%31)-15)*0.125f;
            period[j]=a+adds*b;
        }
        for(size_t off=0;off<size_t(c.n)*groups;off+=h.size()) {
            size_t m=std::min(h.size(),size_t(c.n)*groups-off);
            ACL(aclrtMemcpy(h.data(),m*4,z+off,m*4,ACL_MEMCPY_DEVICE_TO_HOST));
            for(size_t written=0;written<m;) {
                size_t phase=(off+written)%period.size();
                size_t count=std::min(period.size()-phase,m-written);
                std::memcpy(expected.data()+written,period.data()+phase,count*4);
                written+=count;
            }
            // Full bytewise comparison against finite CPU golden values. This avoids
            // recomputing integer remainders for every value of every large sample.
            if(std::memcmp(h.data(),expected.data(),m*4)==0) continue;
            for(size_t j=0;j<m;j++) {
                float a=float(int((off+j)%251)-125)*0.25f;
                float b=float(int((off+j)%31)-15)*0.125f;
                double e=std::abs(h[j]-(a+adds*b));
                if(!std::isfinite(h[j])) throw std::runtime_error("Non-finite output");
                error=std::max(error,e);
            }
        }
        if(error!=0) throw std::runtime_error("Wrong output "+c.id+" error="+std::to_string(error));
        return error;
    }
};

int main(int argc,char** argv) {
    try {
        if(argc<3) throw std::runtime_error("usage: mechanism_bench config.csv output.csv [samples=30] [warmup=10]");
        int samples=argc>3?std::stoi(argv[3]):30, warmup=argc>4?std::stoi(argv[4]):10;
        std::ifstream in(argv[1]); if(!in) throw std::runtime_error("Cannot read configuration");
        std::vector<Config> configs; std::string line; size_t capacity=0;
        while(std::getline(in,line)) {
            if(line.empty()||line[0]=='#'||line.rfind("id,",0)==0) continue;
            std::replace(line.begin(),line.end(),',',' '); std::istringstream s(line); Config c;
            if(!(s>>c.id>>c.mode>>c.n>>c.blocks>>c.tile>>c.buffers>>c.rounds>>c.groups>>c.repeats))
                throw std::runtime_error("Bad config line");
            if(!c.n||!c.blocks||c.blocks>64||!c.tile||c.tile>8192||c.tile%64||c.n%c.blocks||
               (c.n/c.blocks)%c.tile||(c.buffers!=1&&c.buffers!=2)||!c.rounds||!c.groups||!c.repeats)
                throw std::runtime_error("Invalid tile/config: "+c.id);
            if((c.mode=="barrier"||c.mode=="branch") && c.groups>8) throw std::runtime_error("Too many streams");
            // Inputs repeat every 31 floats: this guarantees identical values and
            // outputs in shared/private layouts, while their GM addresses differ.
            if((c.mode=="shared"||c.mode=="private") && ((c.n/c.blocks)%31 || c.buffers!=2))
                throw std::runtime_error("Sharing requires period-aligned lengths and depth 2");
            capacity=std::max(capacity,size_t(c.n)*c.groups); configs.push_back(c);
        }
        if(configs.empty() || capacity>size_t(1)<<29) throw std::runtime_error("Empty/oversized batch");
        std::ofstream out(argv[2]); if(!out) throw std::runtime_error("Cannot open output");
        out<<"id,sample,device_envelope_us,host_us,max_abs_error\n"<<std::setprecision(12);
        Bench bench(capacity);
        for(const auto& c:configs) {
            bench.run(c); bench.verify(c);
            for(int i=0;i<warmup;i++) bench.run(c);
            std::cerr<<"verified "<<c.id<<"\n";
        }
        std::mt19937 rng(20260926);
        for(int rep=0;rep<samples;rep++) {
            std::shuffle(configs.begin(),configs.end(),rng);
            for(const auto& c:configs) {
                auto t=bench.run(c);
                // Check again after each timed sequence, outside its timing window.
                auto error=bench.verify(c);
                out<<c.id<<','<<rep<<','<<t.first<<','<<t.second<<','<<error<<'\n'; out.flush();
            }
        }
        return 0;
    } catch(const std::exception& e) { std::cerr<<e.what()<<'\n'; return 1; }
}
