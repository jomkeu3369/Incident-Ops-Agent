package com.incidentops.backend.domain;

import com.fasterxml.jackson.annotation.JsonInclude;
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
    @JsonInclude(JsonInclude.Include.NON_NULL)
    private String content;
    private Integer sequenceOrder;
    private Integer contentLength;
}
