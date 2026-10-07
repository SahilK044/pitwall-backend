"""Race start: before timing has positions, the leaderboard follows the official grid (GridPos)."""
from live_engine import LiveF1Engine


def test_grid_orders_cars_before_timing_positions():
    e = LiveF1Engine()
    e._apply("DriverList", {"1": {"Tla": "VER"}, "44": {"Tla": "HAM"}, "16": {"Tla": "LEC"}}, snapshot=True)
    e._apply("TimingData", {"Lines": {"1": {}, "44": {}, "16": {}}}, snapshot=True)
    e._apply("TimingAppData", {"Lines": {"1": {"GridPos": "3"}, "44": {"GridPos": "1"}, "16": {"GridPos": "2"}}}, snapshot=True)
    board = e._leaderboard()
    assert [d["driver_number"] for d in board] == [44, 16, 1]
    assert [d["grid_position"] for d in board] == [1, 2, 3]


def test_timing_position_wins_once_it_exists():
    e = LiveF1Engine()
    e._apply("TimingData", {"Lines": {"1": {"Position": "1"}, "44": {"Position": "2"}}}, snapshot=True)
    e._apply("TimingAppData", {"Lines": {"1": {"GridPos": "2"}, "44": {"GridPos": "1"}}}, snapshot=True)
    assert [d["driver_number"] for d in e._leaderboard()] == [1, 44]
