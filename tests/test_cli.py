from fareloot.cli import seed_range_label


def test_seed_label_within_range():
    assert seed_range_label(126, 63, 486) == "tasks 126-189 of 486"


def test_seed_label_splits_when_the_cursor_wraps():
    # Previously printed "tasks 441-504 of 486".
    assert seed_range_label(441, 63, 486) == "tasks 441-486 and 0-18 of 486"


def test_seed_label_ending_exactly_at_total_does_not_split():
    assert seed_range_label(423, 63, 486) == "tasks 423-486 of 486"
