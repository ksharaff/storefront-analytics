"""Which ONE category the pick list shows for an item (2026-09-28).

The store puts every product in many categories, and most of them are
marketing, not what the product is: price bands ("أقل من 100 ريال"), offers
("عروض خاصة", "Buy more for less"), seasons and collections ("المجموعة
الصيفية", "مجموعة رمضان"), "وصل حديثا". A worker walking the storage room
needs what the item IS, so the pick list shows one short name.

SHELF_CATEGORIES lists the product-type categories, in the store's own
wording, MOST SPECIFIC FIRST. An item shows the first one in this list that it
belongs to. An item in none of them keeps its full list, unchanged, so
nothing is guessed: if a long list shows up on the pick list, the fix is to
add its product type here.

Matching is by name (ignoring extra spaces). If the store renames one of
these, its items fall back to the full list, visibly, until it is updated.

Built from the store's category tree on 2026-09-28. Choices:
  * kids' comforter sets show "أطقم لحافات", like all comforter sets;
  * the kids' bathrobe shows "مناشف و ارواب حمام", like the adult robes.
So the kids-only categories ("اطقم لحافات أطفال", "ارواب حمام اطفال", ...)
are deliberately NOT in the list.
"""

SHELF_CATEGORIES = [
    # Each Arabic name is followed by its English twin, so the demo store (English
    # category names) and an Arabic store both work. Rank order is what matters.
    # Order matters where a product sits in two of these:
    "دمى اطفال",              # before مخدات: soft toys are also filed under pillows
    "Kids Plush Toys",
    "أطقم غطاء سرير",         # before أطقم لحافات / لحاف مضغوط: bed covers are filed under both
    "Bed Cover Sets",
    "أطقم لحافات",            # all comforter sets (King, Full, kids, summer, ...)
    "Comforter Sets",
    "حشوة وتلبيسة لحاف",
    "Duvet Inserts",
    "لحاف مضغوط",
    "Compressed Duvets",
    "لباد سرير",
    "Mattress Pads",
    "مخدات",
    "Pillows",
    "بطانيات و شالات",
    "Blankets & Throws",
    "شراشف",
    "Bed Sheets",
    "مناشف و ارواب حمام",     # towels and robes, kids' robes too
    "Towels & Bathrobes",
    "دعاسات",
    "Bath Mats",
    "إكسسوارات حمام",
    "Bath Accessories",
    "أكواب حافظة للحرارة",
    "Insulated Mugs",
    "أطقم فناجين قهوة",
    "Coffee Cup Sets",
    "علب طعام",
    "Food Containers",
    "معطرات مفارش",
    "Linen Sprays",
    "معطرات الحمام",
    "Bathroom Fragrances",
    "معطرات جو للمنازل",
    "Home Fragrances",
    "سليبرات",
    "Slippers",
    "شنط سفر",
    "Travel Bags",
]


def _norm(name: str) -> str:
    """Collapse spaces, so "لباد  سرير" and "لباد سرير" match."""
    return " ".join((name or "").split())


# name -> position in the list (lower wins)
_RANK = {_norm(n): i for i, n in enumerate(SHELF_CATEGORIES)}


def shelf_category(names: list[str] | None) -> str | None:
    """The one category to show for a product with these categories.

    The first SHELF_CATEGORIES entry the product belongs to, in the store's
    spelling. None of them: all its categories, sorted, comma-separated (as
    before). No categories at all: None (the page shows "Uncategorised")."""
    names = sorted({n for n in (names or []) if n})
    if not names:
        return None
    ranked = [(_RANK[_norm(n)], n) for n in names if _norm(n) in _RANK]
    if ranked:
        return min(ranked)[1]
    return ", ".join(names)
