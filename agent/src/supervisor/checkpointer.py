from __future__ import annotations

import base64
from collections import OrderedDict
import json
from math import ceil
import time
from typing import Any, AsyncIterator, Iterator, Sequence
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
)
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError


_REDIS_UNAVAILABLE = (ConnectionError, TimeoutError, RedisConnectionError, RedisTimeoutError)
_RecordKey = tuple[str, str, str]
_Writes = dict[tuple[str, int], tuple[str, list[str]]]


def _encode_typed(serde: Any, value: Any) -> list[str]:
    type_name, raw_bytes = serde.dumps_typed(value)
    return [type_name, base64.b64encode(raw_bytes).decode("ascii")]


def _decode_typed(serde: Any, encoded: Sequence[str]) -> Any:
    return serde.loads_typed((encoded[0], base64.b64decode(encoded[1].encode("ascii"))))


class RedisCheckpointSaver(BaseCheckpointSaver[str]):
    """LangGraph 실행 상태를 Redis(또는 테스트용 인메모리 저장소)에 영속 저장하는 체크포인터."""

    def __init__(
        self,
        redis_client: Any | None = None,
        *,
        ttl_seconds: int = 86400,
        max_mem_threads: int = 256,
        max_mem_checkpoints_per_thread: int = 32,
    ) -> None:
        super().__init__()
        self._redis = redis_client
        self._ttl = ttl_seconds
        self._max_mem_threads = max(1, max_mem_threads)
        self._max_mem_checkpoints = max(1, max_mem_checkpoints_per_thread)
        self._mem_checkpoints: OrderedDict[_RecordKey, tuple[float, str]] = OrderedDict()
        self._mem_writes: OrderedDict[_RecordKey, tuple[float, _Writes]] = OrderedDict()
        self._mem_threads: OrderedDict[str, OrderedDict[_RecordKey, None]] = OrderedDict()
        self._dirty_mem_keys: set[tuple[_RecordKey, bool]] = set()
        self._pending_deleted_threads: dict[str, tuple[float, set[_RecordKey]]] = {}

    def _forget(self, key: _RecordKey, *, writes: bool) -> None:
        cache = self._mem_writes if writes else self._mem_checkpoints
        cache.pop(key, None)
        self._dirty_mem_keys.discard((key, writes))
        thread = self._mem_threads.get(key[0])
        if thread is not None and key not in self._mem_checkpoints and key not in self._mem_writes:
            thread.pop(key, None)
            if not thread:
                self._mem_threads.pop(key[0], None)

    def _prune_mirror(self) -> None:
        now = time.monotonic()
        for cache, writes in ((self._mem_checkpoints, False), (self._mem_writes, True)):
            while cache:
                key = next(iter(cache))
                if cache[key][0] > now:
                    break
                self._forget(key, writes=writes)
        for thread_id, (expiry, _) in list(self._pending_deleted_threads.items()):
            if expiry <= now:
                self._pending_deleted_threads.pop(thread_id, None)

    def _touch(self, key: _RecordKey) -> None:
        thread_id = key[0]
        thread = self._mem_threads.setdefault(thread_id, OrderedDict())
        thread[key] = None
        thread.move_to_end(key)
        self._mem_threads.move_to_end(thread_id)
        while len(thread) > self._max_mem_checkpoints:
            oldest_key = next(iter(thread))
            self._forget(oldest_key, writes=False)
            self._forget(oldest_key, writes=True)
        while len(self._mem_threads) > self._max_mem_threads:
            oldest_thread = next(iter(self._mem_threads))
            for old_key in list(self._mem_threads[oldest_thread]):
                self._forget(old_key, writes=False)
                self._forget(old_key, writes=True)

    def _remember_checkpoint(self, key: _RecordKey, raw: str, *, dirty: bool) -> None:
        self._prune_mirror()
        self._mem_checkpoints[key] = (time.monotonic() + self._ttl, raw)
        self._mem_checkpoints.move_to_end(key)
        if dirty:
            self._dirty_mem_keys.add((key, False))
        self._touch(key)

    def _remember_writes(self, key: _RecordKey, writes: _Writes, *, dirty: bool) -> None:
        self._prune_mirror()
        _, previous = self._mem_writes.get(key) or (0.0, {})
        combined = {**previous}
        for field, value in writes.items():
            if field[1] < 0 or field not in combined:
                combined[field] = value
        self._mem_writes[key] = (time.monotonic() + self._ttl, combined)
        self._mem_writes.move_to_end(key)
        if dirty:
            self._dirty_mem_keys.add((key, True))
        self._touch(key)

    async def _flush_pending_delete(self, thread_id: str) -> None:
        """Redis 단절 중 예약된 스레드 삭제 툼스톤을 Redis 복구 시 선반영한다."""
        deletion = self._pending_deleted_threads.get(thread_id)
        if self._redis is None or deletion is None:
            return
        manifest_key = self._manifest_key(thread_id)
        manifest = await self._redis.hgetall(manifest_key)
        _, remembered = deletion
        recorded = set(remembered)
        recorded.update((thread_id, *json.loads(field)) for field in manifest)
        namespaces = {key[1] for key in recorded} | {""}
        del_keys = [manifest_key]
        for ns in namespaces:
            idx_key = self._index_key(thread_id, ns)
            del_keys.append(idx_key)
            recorded.update((thread_id, ns, str(cid)) for cid in await self._redis.lrange(idx_key, 0, -1))
        for key in recorded:
            del_keys.extend((self._cp_key(*key), self._writes_key(*key)))
        await self._redis.delete(*del_keys)
        if self._pending_deleted_threads.get(thread_id) is deletion:
            self._pending_deleted_threads.pop(thread_id, None)

    @staticmethod
    def _manifest_key(thread_id: str) -> str:
        return f"lg_cp_manifest:{thread_id}"

    async def _register_redis_record(self, key: _RecordKey, ttl: int) -> None:
        manifest_key = self._manifest_key(key[0])
        await self._redis.hset(manifest_key, json.dumps(key[1:]), "1")
        # 기록 여부는 해당 체크포인트나 pending writes보다 먼저 만료되면 안 된다.
        await self._redis.expire(manifest_key, max(ttl, self._ttl) + 1)

    @staticmethod
    def _encode_writes(writes: _Writes) -> dict[str, str]:
        return {
            f"{task_id}:{index}": json.dumps({"task_id": task_id, "channel": channel, "value": value}, ensure_ascii=False)
            for (task_id, index), (channel, value) in writes.items()
        }

    async def _store_redis_writes(self, key: _RecordKey, writes: _Writes, ttl: int) -> None:
        await self._register_redis_record(key, ttl)
        w_key = self._writes_key(*key)
        encoded = self._encode_writes(writes)
        for task_id, index in writes:
            field = f"{task_id}:{index}"
            if index >= 0:
                await self._redis.hsetnx(w_key, field, encoded[field])
            else:
                await self._redis.hset(w_key, field, encoded[field])
        await self._redis.expire(w_key, ttl)

    async def _reconcile_mem_to_redis(self, thread_id: str, checkpoint_ns: str) -> None:
        """일시 Redis 단절 중 로컬 폴백에 쌓인 삭제·체크포인트·독립 쓰기를 Redis 복구 직후 순서대로 반영한다."""
        if self._redis is None:
            return
        self._prune_mirror()
        await self._flush_pending_delete(thread_id)
        if not self._dirty_mem_keys:
            return
        for cache, writes in ((self._mem_checkpoints, False), (self._mem_writes, True)):
            for key, record in list(cache.items()):
                if key[:2] != (thread_id, checkpoint_ns) or (key, writes) not in self._dirty_mem_keys:
                    continue
                expiry, value = record
                ttl = ceil(expiry - time.monotonic())
                if ttl <= 0:
                    continue
                if writes:
                    await self._store_redis_writes(key, value, ttl)
                else:
                    await self._register_redis_record(key, ttl)
                    idx_key = self._index_key(thread_id, checkpoint_ns)
                    existing = await self._redis.lrange(idx_key, 0, -1)
                    await self._redis.set(self._cp_key(*key), value, ex=ttl)
                    if key[2] not in existing:
                        await self._redis.rpush(idx_key, key[2])
                    await self._redis.expire(idx_key, self._ttl)
                if cache.get(key) is record:
                    self._dirty_mem_keys.discard((key, writes))

    @staticmethod
    def _index_key(thread_id: str, checkpoint_ns: str) -> str:
        return f"lg_cp_idx:{thread_id}:{checkpoint_ns}"

    @staticmethod
    def _cp_key(thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"lg_cp:{thread_id}:{checkpoint_ns}:{checkpoint_id}"

    @staticmethod
    def _writes_key(thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"lg_cp_w:{thread_id}:{checkpoint_ns}:{checkpoint_id}"

    def _serialize_record(
        self,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        parent_checkpoint_id: str | None,
    ) -> str:
        payload = {
            "checkpoint": _encode_typed(self.serde, checkpoint),
            "metadata": _encode_typed(self.serde, metadata),
            "parent_checkpoint_id": parent_checkpoint_id,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _deserialize_tuple(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        raw_record: str,
        raw_writes: dict[str, str] | None = None,
    ) -> CheckpointTuple:
        data = json.loads(raw_record)
        checkpoint = _decode_typed(self.serde, data["checkpoint"])
        metadata = _decode_typed(self.serde, data["metadata"])
        parent_id = data.get("parent_checkpoint_id")

        pending_writes: list[tuple[str, str, Any]] = []
        if raw_writes:
            for _, w_raw in sorted(raw_writes.items()):
                w_obj = json.loads(w_raw)
                pending_writes.append((
                    str(w_obj["task_id"]),
                    str(w_obj["channel"]),
                    _decode_typed(self.serde, w_obj["value"]),
                ))

        config: RunnableConfig = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }
        parent_config: RunnableConfig | None = (
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": parent_id,
                }
            }
            if parent_id
            else None
        )
        return CheckpointTuple(
            config=config,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=pending_writes,
        )

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        configurable = config.get("configurable") or {}
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        parent_id = configurable.get("checkpoint_id")
        checkpoint_id = str(checkpoint["id"])
        serialized = self._serialize_record(checkpoint, metadata, str(parent_id) if parent_id else None)

        key = (thread_id, checkpoint_ns, checkpoint_id)
        stored_in_redis = False
        if self._redis is not None:
            try:
                await self._reconcile_mem_to_redis(thread_id, checkpoint_ns)
                await self._register_redis_record(key, self._ttl)
                cp_key = self._cp_key(*key)
                idx_key = self._index_key(thread_id, checkpoint_ns)
                await self._redis.set(cp_key, serialized, ex=self._ttl)
                await self._redis.rpush(idx_key, checkpoint_id)
                await self._redis.expire(idx_key, self._ttl)
                stored_in_redis = True
            except _REDIS_UNAVAILABLE:
                stored_in_redis = False

        self._remember_checkpoint(key, serialized, dirty=self._redis is not None and not stored_in_redis)

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        configurable = config.get("configurable") or {}
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id = str(configurable["checkpoint_id"])

        encoded_items: _Writes = {}
        for idx, (channel, value) in enumerate(writes):
            write_idx = WRITES_IDX_MAP.get(channel, idx)
            encoded_items[(task_id, write_idx)] = (str(channel), _encode_typed(self.serde, value))

        key = (thread_id, checkpoint_ns, checkpoint_id)
        stored_in_redis = False
        if self._redis is not None:
            try:
                await self._reconcile_mem_to_redis(thread_id, checkpoint_ns)
                await self._store_redis_writes(key, encoded_items, self._ttl)
                stored_in_redis = True
            except _REDIS_UNAVAILABLE:
                stored_in_redis = False

        self._remember_writes(key, encoded_items, dirty=self._redis is not None and not stored_in_redis)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        configurable = config.get("configurable") or {}
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        target_id = get_checkpoint_id(config)

        if self._redis is not None:
            try:
                await self._reconcile_mem_to_redis(thread_id, checkpoint_ns)
                redis_target = target_id
                if not redis_target:
                    ids = await self._redis.lrange(self._index_key(thread_id, checkpoint_ns), -1, -1)
                    if ids:
                        redis_target = str(ids[-1])
                if not redis_target:
                    return None
                key = (thread_id, checkpoint_ns, redis_target)
                raw = await self._redis.get(self._cp_key(*key))
                if raw is None:
                    self._forget(key, writes=False)
                    self._forget(key, writes=True)
                    return None
                raw_writes = await self._redis.hgetall(self._writes_key(*key))
                if key in self._mem_checkpoints:
                    self._touch(key)
                return self._deserialize_tuple(*key, raw, raw_writes)
            except _REDIS_UNAVAILABLE:
                pass

        return self._get_mirror(thread_id, checkpoint_ns, target_id)

    def _mirror_ids(self, thread_id: str, checkpoint_ns: str) -> list[str]:
        self._prune_mirror()
        return [cid for tid, ns, cid in reversed(self._mem_checkpoints) if (tid, ns) == (thread_id, checkpoint_ns)]

    def _get_mirror(self, thread_id: str, checkpoint_ns: str, target_id: str | None) -> CheckpointTuple | None:
        self._prune_mirror()
        if target_id is None:
            target_id = next(iter(self._mirror_ids(thread_id, checkpoint_ns)), None)
        if target_id is None:
            return None
        key = (thread_id, checkpoint_ns, target_id)
        record = self._mem_checkpoints.get(key)
        if record is None:
            return None
        self._touch(key)
        _, writes = self._mem_writes.get(key) or (0.0, {})
        return self._deserialize_tuple(*key, record[1], self._encode_writes(writes))

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None or limit == 0:
            return
        configurable = config.get("configurable") or {}
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        before_id = get_checkpoint_id(before) if before else None

        ids: list[str] | None = None
        if self._redis is not None:
            try:
                await self._reconcile_mem_to_redis(thread_id, checkpoint_ns)
                raw_ids = await self._redis.lrange(self._index_key(thread_id, checkpoint_ns), 0, -1)
                ids = [str(x) for x in reversed(raw_ids)]
            except _REDIS_UNAVAILABLE:
                pass
        if ids is None:
            ids = self._mirror_ids(thread_id, checkpoint_ns)

        emitted = 0
        for cid in ids:
            if before_id and cid >= before_id:
                continue
            tup = await self.aget_tuple({
                "configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": cid}
            })
            if tup is None:
                continue
            if filter and any(tup.metadata.get(k) != v for k, v in filter.items()):
                continue
            yield tup
            emitted += 1
            if limit is not None and emitted >= limit:
                break

    async def adelete_thread(self, thread_id: str) -> None:
        self._prune_mirror()
        local_keys = set(self._mem_threads.get(thread_id, ()))
        for key in local_keys:
            self._forget(key, writes=False)
            self._forget(key, writes=True)
        if self._redis is not None:
            _, already_pending = self._pending_deleted_threads.get(thread_id) or (0.0, set())
            self._pending_deleted_threads[thread_id] = (time.monotonic() + self._ttl + 1, local_keys | already_pending)
            try:
                await self._flush_pending_delete(thread_id)
            except _REDIS_UNAVAILABLE:
                pass

    def _require_memory_only(self) -> None:
        if self._redis is not None:
            raise NotImplementedError("RedisCheckpointSaver with Redis requires the async API")

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        self._require_memory_only()
        configurable = config.get("configurable") or {}
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        target_id = get_checkpoint_id(config)
        return self._get_mirror(thread_id, checkpoint_ns, target_id)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        self._require_memory_only()
        if config is None or limit == 0:
            return
        configurable = config.get("configurable") or {}
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        before_id = get_checkpoint_id(before) if before else None
        emitted = 0
        for cid in self._mirror_ids(thread_id, checkpoint_ns):
            if before_id and cid >= before_id:
                continue
            tup = self._get_mirror(thread_id, checkpoint_ns, cid)
            if tup is None:
                continue
            if filter and any(tup.metadata.get(k) != v for k, v in filter.items()):
                continue
            yield tup
            emitted += 1
            if limit is not None and emitted >= limit:
                break

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self._require_memory_only()
        configurable = config.get("configurable") or {}
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        parent_id = configurable.get("checkpoint_id")
        checkpoint_id = str(checkpoint["id"])
        serialized = self._serialize_record(checkpoint, metadata, str(parent_id) if parent_id else None)
        self._remember_checkpoint((thread_id, checkpoint_ns, checkpoint_id), serialized, dirty=False)
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": checkpoint_id}}

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._require_memory_only()
        configurable = config.get("configurable") or {}
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id = str(configurable["checkpoint_id"])
        self._remember_writes(
            (thread_id, checkpoint_ns, checkpoint_id),
            {
                (task_id, WRITES_IDX_MAP.get(channel, idx)): (str(channel), _encode_typed(self.serde, value))
                for idx, (channel, value) in enumerate(writes)
            },
            dirty=False,
        )
