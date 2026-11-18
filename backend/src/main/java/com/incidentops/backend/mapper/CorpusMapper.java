package com.incidentops.backend.mapper;

import com.incidentops.backend.domain.CorpusChunk;
import java.util.List;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

@Mapper
public interface CorpusMapper {
    List<CorpusChunk> searchMetadata(@Param("corpusType") String corpusType, @Param("keyword") String keyword);
    CorpusChunk findByChunkId(@Param("chunkId") String chunkId);
}