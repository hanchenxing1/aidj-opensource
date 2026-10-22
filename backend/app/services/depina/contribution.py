# -*- coding: utf-8 -*-
"""depina.contribution — 贡献日志 + Merkle Root + 离线可验证（MVP 不上链）。

数据结构：
- 每条贡献记录 contribution entry（JSON 一行）：
    {"seq": 1, "ts": 1730000000, "node": "<peerId>", "served_bytes": 1048576,
     "served_peers": ["<id>"], "proof": "<leaf sha256 hex>"}
- 每日封一个 Merkle root（SHA-256 二叉树），leaf = sha256(canonical_json(entry))
- root 由节点密钥签名（MVP 用 Ed25519，与 libp2p 身份同源；链上锚定是未来扩展点）

离线验证：
- 校验 root 签名
- 重算 Merkle 路径（leaf 在 root 中的 path）
- 校验 entry 哈希与路径一致
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def canonical_json(obj: dict) -> str:
    """确定性 JSON 序列化（排序 key，无空白），用于哈希。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass
class Contribution:
    """一条贡献记录。"""
    seq: int
    ts: int          # unix ts
    node: str        # peer id
    served_bytes: int
    served_peers: list[str] = field(default_factory=list)

    def to_entry(self) -> dict:
        entry = {
            "seq": self.seq,
            "ts": self.ts,
            "node": self.node,
            "served_bytes": self.served_bytes,
            "served_peers": self.served_peers,
        }
        entry["proof"] = sha256_hex(canonical_json({k: v for k, v in entry.items() if k != "proof"}))
        return entry

    @classmethod
    def from_entry(cls, entry: dict) -> "Contribution":
        return cls(
            seq=entry["seq"],
            ts=entry["ts"],
            node=entry["node"],
            served_bytes=entry["served_bytes"],
            served_peers=entry.get("served_peers", []),
        )


class MerkleLog:
    """对一组贡献记录计算 Merkle root + 单条证明路径。"""

    @staticmethod
    def leaf_hash(entry: dict) -> str:
        # leaf = sha256(canonical_json(entry 去掉 proof 字段))
        core = {k: v for k, v in entry.items() if k != "proof"}
        return sha256_hex(canonical_json(core))

    @staticmethod
    def build_tree(leaves: list[str]) -> list[list[str]]:
        """自底向上构建 Merkle 树，返回每层的哈希列表（第 0 层 = leaves，最后一层 = [root]）。
        奇数个节点时末尾单节点直接提升（标准 Merkle 约定），不补自身。"""
        if not leaves:
            return []
        layer = list(leaves)
        all_levels: list[list[str]] = [layer]
        while len(layer) > 1:
            nxt: list[str] = []
            i = 0
            while i < len(layer):
                if i + 1 < len(layer):
                    nxt.append(sha256_hex(layer[i] + layer[i + 1]))
                    i += 2
                else:
                    nxt.append(layer[i])  # 单节点直接提升
                    i += 1
            layer = nxt
            all_levels.append(layer)
        return all_levels

    @staticmethod
    def root(leaves: list[str]) -> str:
        levels = MerkleLog.build_tree(leaves)
        return levels[-1][0] if levels else ""

    @staticmethod
    def proof_path(leaves: list[str], idx: int) -> list[tuple[str, bool]]:
        """返回第 idx 个 leaf 的 Merkle 证明路径。
        每项为 (sibling_hash, i_am_left)；sibling_hash 为 None 表示无兄弟（单节点提升层）。
        i_am_left=True 表示当前节点是左子（拼接时当前在左）；False 表示右子（当前在右）。"""
        if idx < 0 or idx >= len(leaves):
            return []
        levels = MerkleLog.build_tree(leaves)
        path: list[tuple[str, bool]] = []
        cur = idx
        for lvl_idx in range(len(levels) - 1):
            lvl = levels[lvl_idx]
            left = (cur // 2) * 2
            right = left + 1
            if cur == left:
                # 我是左子
                sibling = lvl[right] if right < len(lvl) else None
                path.append((sibling, True))
            else:
                # 我是右子
                path.append((lvl[left], False))
            cur = cur // 2
        return path

    @staticmethod
    def verify(leaf_entry: dict, root: str, path: list[tuple[str, bool]]) -> bool:
        """验证单条 entry 在给定 root + 路径下成立。
        path 每项 (sibling, i_am_left)：i_am_left 决定拼接顺序（与 build_tree 一致）。"""
        h = MerkleLog.leaf_hash(leaf_entry)
        for sibling, i_am_left in path:
            if sibling is None:
                continue  # 单节点提升，本层哈希直接作为下一层输入
            if i_am_left:
                h = sha256_hex(h + sibling)
            else:
                h = sha256_hex(sibling + h)
        return h == root


# ── 未来链上锚定扩展点（MVP 不实现，仅留接口） ───────────────────────
def anchor_root_to_chain(root: str, ts: int) -> dict:
    """
    TODO: 把某天的 Merkle root 锚定到联盟链 / L2。
    返回 {"chain": "…", "tx": "…", "root": root}
    MVP 阶段返回空占位。
    """
    return {"chain": "offline", "tx": "", "root": root, "ts": ts}
