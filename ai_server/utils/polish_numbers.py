from __future__ import annotations


ONES = {
    0: "zero",
    1: "jeden",
    2: "dwa",
    3: "trzy",
    4: "cztery",
    5: "pięć",
    6: "sześć",
    7: "siedem",
    8: "osiem",
    9: "dziewięć",
    10: "dziesięć",
    11: "jedenaście",
    12: "dwanaście",
    13: "trzynaście",
    14: "czternaście",
    15: "piętnaście",
    16: "szesnaście",
    17: "siedemnaście",
    18: "osiemnaście",
    19: "dziewiętnaście",
}
TENS = {
    20: "dwadzieścia",
    30: "trzydzieści",
    40: "czterdzieści",
    50: "pięćdziesiąt",
    60: "sześćdziesiąt",
    70: "siedemdziesiąt",
    80: "osiemdziesiąt",
    90: "dziewięćdziesiąt",
}
HUNDREDS = {
    100: "sto",
    200: "dwieście",
    300: "trzysta",
    400: "czterysta",
    500: "pięćset",
    600: "sześćset",
    700: "siedemset",
    800: "osiemset",
    900: "dziewięćset",
}


def polish_cardinal(value: int) -> str:
    if value < 0:
        return f"minus {polish_cardinal(abs(value))}"
    if value < 20:
        return ONES[value]
    if value < 100:
        tens = value // 10 * 10
        remainder = value % 10
        if remainder == 0:
            return TENS[tens]
        return f"{TENS[tens]} {ONES[remainder]}"
    if value < 1000:
        hundreds = value // 100 * 100
        remainder = value % 100
        if remainder == 0:
            return HUNDREDS[hundreds]
        return f"{HUNDREDS[hundreds]} {polish_cardinal(remainder)}"
    return str(value)


def polish_decimal(value: float) -> str:
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    if "." not in text:
        return polish_cardinal(int(text))
    integer_text, fraction_text = text.split(".", 1)
    integer = polish_cardinal(int(integer_text))
    fraction = " ".join(polish_cardinal(int(digit)) for digit in fraction_text)
    return f"{integer} przecinek {fraction}"
