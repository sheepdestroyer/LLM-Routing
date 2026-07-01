# Triage Upgrade Plan

## Overview
This document outlines the steps to upgrade the triage system in the LLM-Routing backend.

## Steps

### 1. Review Current Triage Logic
- Examine the existing triage implementation in `router/agy_proxy.py` and `router/main.py`.
- Identify the current routing rules and classifier usage.

### 2. Update Classifier Model
- Replace the current classifier with a newer version (e.g., a fine-tuned model on recent data).
- Ensure the new classifier is compatible with the existing input/output format.

### 3. Improve Feature Extraction
- Enhance the feature extraction process to include more contextual information.
- Consider adding features such as request length, historical routing success, and user feedback.

### 4. A/B Testing Framework
- Implement an A/B testing framework to compare the performance of the old and new triage systems.
- Use a small percentage of traffic for the new system and monitor key metrics (accuracy, latency, user satisfaction).

### 5. Rollout Strategy
- Gradually increase the traffic to the new triage system based on A/B test results.
- Have a rollback plan in case of performance degradation.

### 6. Monitoring and Alerting
- Set up monitoring for the triage system's performance metrics.
- Configure alerts for anomalies in routing accuracy or latency.

### 7. Documentation Update
- Update the documentation to reflect the changes in the triage system.
- Include instructions for future updates and maintenance.

## Files to Modify
- `router/agy_proxy.py`: Contains the core triage logic.
- `router/main.py`: Entry point that may call the triage functions.
- `router/config.yaml`: Configuration for the classifier and triage parameters.
- `test_agy_tiers.py`: Tests for the triage tiers, may need updates.
- `test_classifier_accuracy.py`: Tests for classifier accuracy, may need updates.

## Estimated Effort
- Review and planning: 2 hours
- Implementation: 8 hours
- Testing and A/B setup: 4 hours
- Rollout and monitoring: 4 hours
- Documentation: 2 hours
Total: 20 hours

## Risks
- New classifier may have lower accuracy on edge cases.
- Increased latency due to more complex feature extraction.
- A/B testing may not be statistically significant if traffic is low.

## Mitigations
- Validate the new classifier on a held-out dataset before deployment.
- Optimize feature extraction to minimize latency impact.
- Use statistical significance testing for A/B tests and consider Bayesian approaches for low traffic.

## Conclusion
This plan aims to improve the triage system's accuracy and reliability while minimizing disruption to the service.