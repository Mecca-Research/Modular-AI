BCIR_C := ../../../Binary-Correspondence-Intermediate-Representation/runtime/c
CC ?= cc
CFLAGS := -std=c11 -O3 -ffp-contract=off -fno-math-errno -Wall -Wextra -Wpedantic -Werror -I$(BCIR_C)
LDFLAGS := -lm -pthread

SRCS := $(BCIR_C)/bcir_tensor.c $(BCIR_C)/bcir_precision.c $(BCIR_C)/bcir_mixer_ext.c

.PHONY: all verify clean

all: build/libbcir_mixer_ext.so build/test_mixer_ext

build:
	mkdir -p build

build/libbcir_mixer_ext.so: $(SRCS) | build
	$(CC) $(CFLAGS) -DBCIR_DT_BUILD_SHARED -shared -fPIC $(SRCS) -o $@ $(LDFLAGS)

build/test_mixer_ext: $(SRCS) $(BCIR_C)/test_mixer_ext.c | build
	$(CC) $(CFLAGS) $(SRCS) $(BCIR_C)/test_mixer_ext.c -o $@ $(LDFLAGS)

verify: build/test_mixer_ext
	./build/test_mixer_ext

clean:
	rm -rf build src/__pycache__
