from __future__ import annotations

import base64
import json
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


def _encode_typed(serde: Any, value: Any) -> list[str]:
    type_name, raw_bytes = serde.dumps_typed(value)
    return [type_name, base64.b64encode(raw_bytes).decode("ascii")]


def _decode_typed(serde: Any, encoded: Sequence[str]) -> Any:
    return serde.loads_typed((encoded[0], base64.b64decode(encoded[1].encode("ascii"))))


class RedisCheckpointSaver(BaseCheckpointSaver[str]):
    """LangGraph 실행 상태를 Redis(또는 테스트용 인메모리 저장소)에 영속 저장하는 체크포인터."""

    def __init__(self, redis_client: Any | None = None, *, ttl_seconds: int = 86400) -> None:
        super().__init__()
        self._redis = redis_client
        self._ttl = ttl_seconds
        # Redis 미연결·오프라인 테스트 시에도 프로세스 내 재개를 지원하는 폴백 저장소 및 리컨사일 추적
        self._mem_checkpoints: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._mem_writes: dict[tuple[str, str, str], dict[tuple[str, int], tuple[str, str, list[str]]]] = {}
        self._dirty_mem_keys: set[tuple[str, str]] = set()
        self._pending_deleted_threads: set[str] = set()

    async def _flush_pending_delete(self, thread_id: str) -> None:
        """Redis 단절 중 예약된 스레드 삭제 툼스톤을 Redis 복구 시 선반영한다."""
        if self._redis is None or thread_id not in self._pending_deleted_threads:
            return
        idx_key = self._index_key(thread_id, "")
        cids = await self._redis.lrange(idx_key, 0, -1)
        del_keys = [idx_key]
        for cid in cids:
            del_keys.append(self._cp_key(thread_id, "", str(cid)))
            del_keys.append(self._writes_key(thread_id, "", str(cid)))
        if del_keys:
            await self._redis.delete(*del_keys)
        self._pending_deleted_threads.discard(thread_id)

    async def _reconcile_mem_to_redis(self, thread_id: str, checkpoint_ns: str) -> None:
        """일시 Redis 단절 중 로컬 폴백에 쌓인 삭제·체크포인트·독립 쓰기를 Redis 복구 직후 순서대로 반영한다."""
        if self._redis is None:
            return
        await self._flush_pending_delete(thread_id)
        key = (thread_id, checkpoint_ns)
        if key not in self._dirty_mem_keys:
            return
        bucket = self._mem_checkpoints.get(key) or []
        idx_key = self._index_key(thread_id, checkpoint_ns)
        existing_ids = set(str(x) for x in (await self._redis.lrange(idx_key, 0, -1)))
        reconciled_write_cids: set[str] = set()

        for cid, serialized in bucket:
            cp_key = self._cp_key(thread_id, checkpoint_ns, cid)
            await self._redis.set(cp_key, serialized, ex=self._ttl)
            if cid not in existing_ids:
                await self._redis.rpush(idx_key, cid)
                existing_ids.add(cid)

        # 체크포인트 본체는 이미 Redis에 있고 후속 aput_writes만 폴백된 경우까지 누락 없이 독립 리컨사일한다
        for (t_id, ns, cid), mem_w in list(self._mem_writes.items()):
            if (t_id, ns) != key or not mem_w:
                continue
            w_key = self._writes_key(thread_id, checkpoint_ns, cid)
            for (tid_w, write_idx), (_, channel, val_enc) in mem_w.items():
                field = f"{tid_w}:{write_idx}"
                raw_val = json.dumps({"task_id": tid_w, "channel": channel, "value": val_enc}, ensure_ascii=False)
                if write_idx >= 0:
                    await self._redis.hsetnx(w_key, field, raw_val)
                else:
                    await self._redis.hset(w_key, field, raw_val)
            await self._redis.expire(w_key, self._ttl)
            reconciled_write_cids.add(cid)

        if bucket or reconciled_write_cids:
            await self._redis.expire(idx_key, self._ttl)
        self._dirty_mem_keys.discard(key)

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

        stored_in_redis = False
        if self._redis is not None:
            try:
                await self._reconcile_mem_to_redis(thread_id, checkpoint_ns)
                cp_key = self._cp_key(thread_id, checkpoint_ns, checkpoint_id)
                idx_key = self._index_key(thread_id, checkpoint_ns)
                await self._redis.set(cp_key, serialized, ex=self._ttl)
                await self._redis.rpush(idx_key, checkpoint_id)
                await self._redis.expire(idx_key, self._ttl)
                stored_in_redis = True
            except Exception:
                stored_in_redis = False

        if not stored_in_redis:
            bucket = self._mem_checkpoints.setdefault((thread_id, checkpoint_ns), [])
            bucket.append((checkpoint_id, serialized))
            self._dirty_mem_keys.add((thread_id, checkpoint_ns))

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

        encoded_items: list[tuple[int, str, list[str]]] = []
        for idx, (channel, value) in enumerate(writes):
            write_idx = WRITES_IDX_MAP.get(channel, idx)
            encoded_items.append((write_idx, str(channel), _encode_typed(self.serde, value)))

        stored_in_redis = False
        if self._redis is not None:
            try:
                await self._reconcile_mem_to_redis(thread_id, checkpoint_ns)
                w_key = self._writes_key(thread_id, checkpoint_ns, checkpoint_id)
                for write_idx, channel, val_enc in encoded_items:
                    field = f"{task_id}:{write_idx}"
                    raw_val = json.dumps({"task_id": task_id, "channel": channel, "value": val_enc}, ensure_ascii=False)
                    if write_idx >= 0:
                        await self._redis.hsetnx(w_key, field, raw_val)
                    else:
                        await self._redis.hset(w_key, field, raw_val)
                await self._redis.expire(w_key, self._ttl)
                stored_in_redis = True
            except Exception:
                stored_in_redis = False

        if not stored_in_redis:
            mem_w = self._mem_writes.setdefault((thread_id, checkpoint_ns, checkpoint_id), {})
            for write_idx, channel, val_enc in encoded_items:
                key = (task_id, write_idx)
                if write_idx >= 0 and key in mem_w:
                    continue
                mem_w[key] = (task_id, channel, val_enc)
            self._dirty_mem_keys.add((thread_id, checkpoint_ns))

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
                if redis_target:
                    raw = await self._redis.get(self._cp_key(thread_id, checkpoint_ns, redis_target))
                    if raw:
                        raw_writes = await self._redis.hgetall(self._writes_key(thread_id, checkpoint_ns, redis_target))
                        return self._deserialize_tuple(thread_id, checkpoint_ns, redis_target, raw, raw_writes)
            except Exception:
                pass

        bucket = self._mem_checkpoints.get((thread_id, checkpoint_ns)) or []
        if not bucket:
            return None
        if target_id:
            matched = next(((cid, raw) for cid, raw in reversed(bucket) if cid == target_id), None)
        else:
            matched = bucket[-1]
        if matched is None:
            return None
        cid, raw = matched
        mem_w = self._mem_writes.get((thread_id, checkpoint_ns, cid)) or {}
        raw_writes = {
            f"{tid}:{idx}": json.dumps({"task_id": tid, "channel": ch, "value": val_enc}, ensure_ascii=False)
            for (tid, idx), (_, ch, val_enc) in mem_w.items()
        }
        return self._deserialize_tuple(thread_id, checkpoint_ns, cid, raw, raw_writes)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            return
        configurable = config.get("configurable") or {}
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        before_id = get_checkpoint_id(before) if before else None

        ids: list[str] = []
        if self._redis is not None:
            try:
                await self._reconcile_mem_to_redis(thread_id, checkpoint_ns)
                raw_ids = await self._redis.lrange(self._index_key(thread_id, checkpoint_ns), 0, -1)
                ids = [str(x) for x in reversed(raw_ids)]
            except Exception:
                ids = []
        if not ids:
            bucket = self._mem_checkpoints.get((thread_id, checkpoint_ns)) or []
            ids = [cid for cid, _ in reversed(bucket)]

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
        for key in list(self._mem_checkpoints.keys()):
            if key[0] == thread_id:
                del self._mem_checkpoints[key]
        for key in list(self._mem_writes.keys()):
            if key[0] == thread_id:
                del self._mem_writes[key]
        for key in list(self._dirty_mem_keys):
            if key[0] == thread_id:
                self._dirty_mem_keys.discard(key)
        if self._redis is not None:
            try:
                self._pending_deleted_threads.add(thread_id)
                await self._flush_pending_delete(thread_id)
            except Exception:
                # Redis 일시 단절 시 삭제 툼스톤을 유지해 복구 직후 기존 체크포인트가 부활하지 못하게 막는다
                self._pending_deleted_threads.add(thread_id)

    # 동기 추상 메서드 호환 구현 (비동기 전용 호출 권장)
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        configurable = config.get("configurable") or {}
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        target_id = get_checkpoint_id(config)
        bucket = self._mem_checkpoints.get((thread_id, checkpoint_ns)) or []
        if not bucket:
            return None
        matched = next(((cid, raw) for cid, raw in reversed(bucket) if cid == target_id), None) if target_id else bucket[-1]
        if matched is None:
            return None
        cid, raw = matched
        return self._deserialize_tuple(thread_id, checkpoint_ns, cid, raw)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if config is None:
            return
        configurable = config.get("configurable") or {}
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        bucket = self._mem_checkpoints.get((thread_id, checkpoint_ns)) or []
        emitted = 0
        for cid, raw in reversed(bucket):
            tup = self._deserialize_tuple(thread_id, checkpoint_ns, cid, raw)
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
        configurable = config.get("configurable") or {}
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        parent_id = configurable.get("checkpoint_id")
        checkpoint_id = str(checkpoint["id"])
        serialized = self._serialize_record(checkpoint, metadata, str(parent_id) if parent_id else None)
        self._mem_checkpoints.setdefault((thread_id, checkpoint_ns), []).append((checkpoint_id, serialized))
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": checkpoint_id}}

    def put_writes(
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
        mem_w = self._mem_writes.setdefault((thread_id, checkpoint_ns, checkpoint_id), {})
        for idx, (channel, value) in enumerate(writes):
            write_idx = WRITES_IDX_MAP.get(channel, idx)
            key = (task_id, write_idx)
            if write_idx >= 0 and key in mem_w:
                continue
            mem_w[key] = (task_id, str(channel), _encode_typed(self.serde, value))
