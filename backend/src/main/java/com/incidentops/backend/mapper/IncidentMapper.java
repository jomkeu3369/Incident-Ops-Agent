package com.incidentops.backend.mapper;

import com.incidentops.backend.domain.Incident;
import java.util.List;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

@Mapper
public interface IncidentMapper {
    List<Incident> findAll();
    Incident findByIncidentCode(@Param("incidentCode") String incidentCode);
    Incident lockByIncidentCode(@Param("incidentCode") String incidentCode);
    int insert(Incident incident);
    int updateStatus(@Param("incidentCode") String incidentCode, @Param("status") String status);
}
