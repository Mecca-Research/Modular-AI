BCIR_C := ../../../Binary-Correspondence-Intermediate-Representation/runtime/c
CC     ?= clang
CFLAGS := -std=c11 -O3 -ffp-contract=off -fno-math-errno -Wall -Wextra -Wpedantic -Werror -I$(BCIR_C)
LDFLAGS := -lm -pthread

BUILD  := build
LIB    := $(BUILD)/libbcir_mixer_ext.so
TEST   := $(BUILD)/test_mixer_ext

SRCS := $(BCIR_C)/bcir_tensor.c $(BCIR_C)/bcir_precision.c $(BCIR_C)/bcir_mixer_ext.c

.PHONY: all verify clean

all: $(LIB)

$(BUILD):
	mkdir -p $(BUILD)

$(LIB): $(SRCS) | $(BUILD)
	$(CC) $(CFLAGS) -DBCIR_DT_BUILD_SHARED -shared -fPIC $(SRCS) -o $(LIB) $(LDFLAGS)

verify: $(SRCS) $(BCIR_C)/test_mixer_ext.c | $(BUILD)
	$(CC) $(CFLAGS) $(BCIR_C)/test_mixer_ext.c $(SRCS) -o $(TEST) $(LDFLAGS)
	./$(TEST)

clean:
	rm -rf $(BUILD)
