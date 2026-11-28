BCIR_C := ../../../Binary-Correspondence-Intermediate-Representation/runtime/c
CC ?= cc
CFLAGS := -std=c11 -O3 -mavx2 -ffp-contract=off -fno-math-errno -Wall -Wextra -Wpedantic -Werror -I$(BCIR_C) -Isrc
LDFLAGS := -lm -pthread

BCIR_SRCS := $(BCIR_C)/bcir_tensor.c $(BCIR_C)/bcir_precision.c $(BCIR_C)/bcir_mixer_ext.c
PROBE_SRCS := src/bcir_stateful_probe.c

.PHONY: all verify clean

all: build/libbcir_wsp_sim.so build/test_mixer_ext

build:
	mkdir -p build

build/libbcir_wsp_sim.so: $(BCIR_SRCS) $(PROBE_SRCS) src/bcir_stateful_probe.h | build
	$(CC) $(CFLAGS) -DBCIR_DT_BUILD_SHARED -shared -fPIC $(BCIR_SRCS) $(PROBE_SRCS) -o $@ $(LDFLAGS)

build/test_mixer_ext: $(BCIR_SRCS) $(BCIR_C)/test_mixer_ext.c | build
	$(CC) $(CFLAGS) $(BCIR_SRCS) $(BCIR_C)/test_mixer_ext.c -o $@ $(LDFLAGS)

verify: build/test_mixer_ext
	./build/test_mixer_ext

clean:
	rm -rf build src/__pycache__
