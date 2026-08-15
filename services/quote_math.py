def quote_line_amounts(
    quantity: float,
    unit_price: float,
    gst_rate: float,
    gst_inclusive: bool,
) -> tuple[float, float, float]:
    entered = round(quantity * unit_price, 2)
    if gst_inclusive and gst_rate:
        base = round(entered / (1 + gst_rate / 100), 2)
        gst = round(entered - base, 2)
    else:
        base = entered
        gst = 0 if gst_inclusive else round(base * gst_rate / 100, 2)
    return base, gst, entered if gst_inclusive else round(base + gst, 2)
