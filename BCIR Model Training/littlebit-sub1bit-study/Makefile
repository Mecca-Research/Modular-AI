# Portable Makefile for BCIR Bare-Metal C LittleBit / LittleBit-2 Evaluation
#
# Usage:
#   BCIR_REPO=/path/to/Binary-Correspondence-Intermediate-Representation make verify
#   BCIR_REPO=/path/to/Binary-Correspondence-Intermediate-Representation make bench-packed
#
# Targets:
#   all           Build build/libbcir_littlebit.so, build/bcir_decoder_train.so, build/bench_lut4_packed
#   verify        Run all 3 verification gates (Dense vs BCIR, Finite-Diff, AVX-512 Packed-1b LUT4)
#   bench-packed  Run the batched Packed-1b LUT4 prefill/decode sweep across shapes
#   clean         Remove build/ artifacts

BCIR_REPO ?= ../../../Binary-Correspondence-Intermediate-Representation
ifeq ($(origin CC),default)
CC        := clang
endif
PYTHON    ?= python3
CFLAGS    ?= -O3 -std=c11 -D_POSIX_C_SOURCE=200809L -march=native -ffp-contract=off
LDFLAGS   ?= -lm -lpthread

BUILD_DIR := build

.PHONY: all verify bench-packed clean

all: $(BUILD_DIR)/libbcir_littlebit.so $(BUILD_DIR)/bcir_decoder_train.so $(BUILD_DIR)/bench_lut4_packed

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/libbcir_littlebit.so: src/bcir_littlebit.h src/bcir_littlebit_core.c src/bcir_littlebit_bench.c $(BCIR_REPO)/runtime/c/bcir_tensor.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -fPIC -shared \
		-I"$(BCIR_REPO)/runtime/c" -Isrc \
		"$(BCIR_REPO)/runtime/c/bcir_tensor.c" \
		src/bcir_littlebit_core.c \
		src/bcir_littlebit_bench.c \
		-o $@ $(LDFLAGS)

$(BUILD_DIR)/bcir_decoder_train.so: $(BCIR_REPO)/runtime/c/bcir_tensor.c $(BCIR_REPO)/runtime/c/bcir_decoder_train.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -fPIC -shared \
		-I"$(BCIR_REPO)/runtime/c" \
		"$(BCIR_REPO)/runtime/c/bcir_tensor.c" \
		"$(BCIR_REPO)/runtime/c/bcir_decoder_train.c" \
		-o $@ $(LDFLAGS)

$(BUILD_DIR)/bench_lut4_packed: src/bench_lut4_packed.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) src/bench_lut4_packed.c -o $@ $(LDFLAGS)

verify: all
	BCIR_REPO="$(BCIR_REPO)" $(PYTHON) src/lb_harness.py

bench-packed: all
	BCIR_REPO="$(BCIR_REPO)" $(PYTHON) src/pillar5_packed_batched_bench.py

clean:
	rm -rf $(BUILD_DIR)
