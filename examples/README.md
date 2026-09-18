# Example Specifications

Use these with `--spec` to quickly test the pipeline.

## 4-bit Counter

```bash
python agentic_rtl_coder.py --spec "4-bit synchronous up-counter with asynchronous active-low reset. The counter should increment on every rising clock edge when enable is high. Outputs: 4-bit count value."
```

## 8-bit ALU

```bash
python agentic_rtl_coder.py --spec "8-bit ALU with 4 operations selected by a 2-bit opcode: 00=add, 01=subtract, 10=bitwise AND, 11=bitwise OR. Inputs: two 8-bit operands and 2-bit opcode. Outputs: 8-bit result and carry-out flag."
```

## Simple FIFO

```bash
python agentic_rtl_coder.py --spec "Synchronous FIFO with 8 entries of 8-bit data. Signals: clk, rst, wr_en, rd_en, data_in[7:0], data_out[7:0], full, empty."
```
