package com.incidentops.backend.domain;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class CorpusChunk {
    private Long id;
    private String chunkId;
    private String corpusType;
    private String serviceName;
    private String title;
    private String keywords;
    private String content;
    private Integer sequenceOrder;
    private Integer contentLength;
}