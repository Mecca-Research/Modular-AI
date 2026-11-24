# Portable build & verification Makefile for BCIR Precision Policy (P1-P6),
# TRAIN-OPT-1/2/4/6/7, MIXER-EXT (GQA, SWA, Arch-1 DeltaNet, Arch-2 Hybrid-SWA,
# Arch-4 MLA), and Whole-Model COMP-1 Deployment Study.
ifeq ($(origin CC),default)
  CC := clang
endif
BCIR_REPO ?= $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/../../../Binary-Correspondence-Intermediate-Representation)
BCIR_C    := $(BCIR_REPO)/runtime/c
SRC       := src
BUILD     := build
CFLAGS    := -O3 -std=c11 -D_POSIX_C_SOURCE=200809L -march=native -ffp-contract=off -Wall -Wextra -Werror
INC       := -I$(BCIR_C) -I$(SRC)
LDFLAGS   := -lm -lpthread

.PHONY: all verify clean

all: $(BUILD)/libbcir_lab.so $(BUILD)/test_gate0_all $(BUILD)/bcir_native_deploy_bench

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/bcir_tensor.o: $(BCIR_C)/bcir_tensor.c | $(BUILD)
	$(CC) -O3 -std=c11 -D_POSIX_C_SOURCE=200809L -march=native -ffp-contract=off -fPIC -I$(BCIR_C) -c $< -o $@

$(BUILD)/bcir_decoder_train.o: $(BCIR_C)/bcir_decoder_train.c | $(BUILD)
	$(CC) -O3 -std=c11 -D_POSIX_C_SOURCE=200809L -march=native -ffp-contract=off -fPIC -I$(BCIR_C) -c $< -o $@

$(BUILD)/libbcir_lab.so: $(SRC)/bcir_precision.c $(SRC)/bcir_mixer_ext.c $(SRC)/bcir_comp1_deploy.c \
                         $(SRC)/bcir_littlebit_core.c $(SRC)/bcir_littlebit_bench.c \
                         $(BUILD)/bcir_tensor.o $(BUILD)/bcir_decoder_train.o | $(BUILD)
	$(CC) $(CFLAGS) -fPIC -shared $(INC) $^ $(LDFLAGS) -o $@

$(BUILD)/test_gate0_all: $(SRC)/test_gate0_all.c $(SRC)/bcir_mixer_ext.c $(SRC)/bcir_precision.c \
                         $(BUILD)/bcir_tensor.o $(BUILD)/bcir_decoder_train.o | $(BUILD)
	$(CC) $(CFLAGS) $(INC) $^ $(LDFLAGS) -o $@

$(BUILD)/bcir_native_deploy_bench: $(SRC)/bcir_native_deploy_bench.c \
                                   $(BCIR_C)/bcir_q8_model.c $(BCIR_C)/bcir_decode.c \
                                   $(BCIR_C)/bcir_ai_kernels.c $(BCIR_C)/bcir_llama.c \
                                   $(BCIR_C)/bcir_tensor.c | $(BUILD)
	$(CC) -O3 -std=c11 -D_POSIX_C_SOURCE=200809L -ffp-contract=off -Wall -Wextra -Werror $(INC) $^ $(LDFLAGS) -o $@

verify: $(BUILD)/test_gate0_all
	./$(BUILD)/test_gate0_all

clean:
	rm -rf $(BUILD)
