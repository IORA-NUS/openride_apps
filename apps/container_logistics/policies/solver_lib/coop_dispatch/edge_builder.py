from __future__ import annotations

from datetime import timedelta
from itertools import product
from typing import Dict, Iterable, List, Tuple, Any


class EdgeBuilder:
    """Build feasible edge sets and edge costs for the solver graph."""

    def __init__(self, travel_lookup: Dict[Tuple[str, str], Dict[str, Any]]):
        self.travel_lookup = travel_lookup

    def get_detour_distance(
        self,
        from_i: str,
        to_j: str,
        parking_yds: Iterable[str],
    ) -> float:
        min_detour = float("inf")
        for yd in parking_yds:
            entry1 = self.travel_lookup.get((from_i, yd))
            entry2 = self.travel_lookup.get((yd, to_j))
            if entry1 is None or entry2 is None:
                continue
            detour = entry1["distance"] + entry2["distance"]
            min_detour = min(min_detour, detour)
        return min_detour

    def build_edges_with_bad_exchange(
        self,
        raw_edge_list: List[Tuple[int, int]],
        order_list: List[int],
        order_owner: Dict[int, str],
        order_info: Dict[int, Dict[str, Any]],
        parking_yds: List[str],
        bad_wait_min: float = 120,
        bad_deadhead_km: float = 5,
        wait_limit_min: float = 120,
        always_include_raw: bool = True,
    ):
        """Build feasible edges while flagging bad orders from raw sequences."""

        prev_of, next_of = {}, {}
        for a, b in raw_edge_list:
            next_of[a] = b
            prev_of[b] = a

        w = timedelta(minutes=float(bad_wait_min))
        lm = float(bad_deadhead_km) * 1000.0

        bad_in = set()
        bad_out = set()

        def _check_pair_and_mark(o_prev, o_curr):
            try:
                p = order_info[o_prev]
                c = order_info[o_curr]
                td = self.travel_lookup.get((p["delivery_postal_code"], c["pickup_postal_code"]))
                if not td:
                    return
                travel_time = timedelta(seconds=float(td["duration"]))
                travel_dist = float(td["distance"])
                arrival = p["actual_end_time"] + travel_time
                wait = c["actual_start_time"] - arrival
                if wait < timedelta(0):
                    wait = timedelta(0)
                if (wait > w) or (travel_dist > lm):
                    bad_in.add(o_curr)
                    bad_out.add(o_prev)
            except KeyError:
                return

        for a, b in raw_edge_list:
            _check_pair_and_mark(a, b)

        bad_orders = bad_in & bad_out

        wait_limit = timedelta(minutes=float(wait_limit_min))
        edges = []
        edge_cost = {}
        edge_trailer_cost = {}

        for o1, o2 in product(order_list, repeat=2):
            if o1 == o2:
                continue
            try:
                i1 = order_info[o1]
                i2 = order_info[o2]
                end_dt_1 = i1["actual_end_time"]
                start_dt_2 = i2["actual_start_time"]
                loc1 = i1["delivery_postal_code"]
                loc2 = i2["pickup_postal_code"]
                trailer_change_flag = i1["occupied_job"] * (1 - i2["released_job"])

                td = self.travel_lookup.get((loc1, loc2))
                if not td:
                    continue

                travel_time = timedelta(seconds=float(td["duration"]))
                arrival_time = end_dt_1 + travel_time

                if not (arrival_time <= start_dt_2 and (start_dt_2 - arrival_time) <= wait_limit):
                    continue

                same_company = order_owner[o1] == order_owner[o2]
                is_bad_pair = (o1 in bad_orders) or (o2 in bad_orders)

                if same_company or (not same_company and is_bad_pair):
                    edges.append((o1, o2))
                    edge_cost[(o1, o2)] = float(td["distance"])
                    if trailer_change_flag == 1:
                        edge_trailer_cost[(o1, o2)] = self.get_detour_distance(loc1, loc2, parking_yds)
                    else:
                        edge_trailer_cost[(o1, o2)] = 0
            except KeyError:
                continue

        if always_include_raw:
            edges_set = set(edges)
            for a, b in raw_edge_list:
                trailer_change_flag = order_info[a]["occupied_job"] * (1 - order_info[b]["released_job"])
                if (a, b) not in edges_set:
                    edges.append((a, b))
                    edges_set.add((a, b))
                if (a, b) not in edge_cost:
                    try:
                        loc1 = order_info[a]["delivery_postal_code"]
                        loc2 = order_info[b]["pickup_postal_code"]
                        td = self.travel_lookup.get((loc1, loc2))
                        if td:
                            edge_cost[(a, b)] = float(td["distance"])
                            if trailer_change_flag == 1:
                                edge_trailer_cost[(a, b)] = self.get_detour_distance(loc1, loc2, parking_yds)
                            else:
                                edge_trailer_cost[(a, b)] = 0
                    except KeyError:
                        pass

        return bad_orders, edges, edge_cost, edge_trailer_cost
