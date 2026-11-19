package com.incidentops.backend.mapper;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.incidentops.backend.domain.RemediationPlanStep;
import java.sql.CallableStatement;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.List;
import org.apache.ibatis.type.BaseTypeHandler;
import org.apache.ibatis.type.JdbcType;

public class PlanStepsTypeHandler extends BaseTypeHandler<List<RemediationPlanStep>> {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final TypeReference<List<RemediationPlanStep>> STEPS_TYPE = new TypeReference<>() {};

    @Override
    public void setNonNullParameter(
            PreparedStatement statement, int index, List<RemediationPlanStep> steps, JdbcType jdbcType
    ) throws SQLException {
        try {
            statement.setString(index, JSON.writeValueAsString(steps));
        } catch (JsonProcessingException ex) {
            throw new SQLException("복구 계획 단계를 JSON으로 변환할 수 없습니다.", ex);
        }
    }

    @Override
    public List<RemediationPlanStep> getNullableResult(ResultSet result, String column) throws SQLException {
        return readSteps(result.getString(column));
    }

    @Override
    public List<RemediationPlanStep> getNullableResult(ResultSet result, int column) throws SQLException {
        return readSteps(result.getString(column));
    }

    @Override
    public List<RemediationPlanStep> getNullableResult(CallableStatement statement, int column) throws SQLException {
        return readSteps(statement.getString(column));
    }

    private List<RemediationPlanStep> readSteps(String json) throws SQLException {
        if (json == null) {
            return null;
        }
        try {
            return JSON.readValue(json, STEPS_TYPE);
        } catch (JsonProcessingException ex) {
            throw new SQLException("저장된 복구 계획 단계를 읽을 수 없습니다.", ex);
        }
    }
}
