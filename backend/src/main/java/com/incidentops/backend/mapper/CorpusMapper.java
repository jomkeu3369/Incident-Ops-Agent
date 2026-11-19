package com.incidentops.backend.mapper;

import com.incidentops.backend.domain.CorpusChunk;
import java.util.List;
import java.util.Set;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

@Mapper
public interface CorpusMapper {
    List<CorpusChunk> searchMetadata(
            @Param("incidentCode") String incidentCode,
            @Param("serviceName") String serviceName,
            @Param("corpusType") String corpusType,
            @Param("keyword") String keyword
    );
    CorpusChunk findByChunkId(
            @Param("incidentCode") String incidentCode,
            @Param("serviceName") String serviceName,
            @Param("chunkId") String chunkId
    );
    List<String> findScopedChunkIds(
            @Param("incidentCode") String incidentCode,
            @Param("serviceName") String serviceName,
            @Param("chunkIds") Set<String> chunkIds
    );
}
