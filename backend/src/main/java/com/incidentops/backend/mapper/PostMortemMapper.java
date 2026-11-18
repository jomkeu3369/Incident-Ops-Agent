package com.incidentops.backend.mapper;

import com.incidentops.backend.domain.PostMortem;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

@Mapper
public interface PostMortemMapper {
    PostMortem findByIncidentCode(@Param("incidentCode") String incidentCode);
    int insert(PostMortem postMortem);
}