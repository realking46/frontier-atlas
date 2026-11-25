// Jenkins pipeline: install, run the pytest suite with coverage, publish results,
// and (optionally) run a SonarQube analysis.
// Agent needs Python 3.11+. Plugins: JUnit, Coverage (for Cobertura XML),
// and SonarQube Scanner if you enable the quality stage.
pipeline {
    agent any

    options {
        timeout(time: 20, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    triggers {
        pollSCM('H/15 * * * *')   // or use a GitHub webhook instead
    }

    parameters {
        booleanParam(name: 'RUN_SONAR', defaultValue: false,
                     description: 'Run SonarQube analysis (needs a server named "sonarqube" in Jenkins config)')
    }

    stages {
        stage('Install') {
            steps {
                sh '''
                    python3 -m venv .venv
                    . .venv/bin/activate
                    pip install --upgrade pip
                    pip install -e ".[dev]"
                '''
            }
        }

        stage('Test') {
            steps {
                sh '''
                    . .venv/bin/activate
                    pytest -q \
                      --cov=frontier_atlas --cov-report=xml:coverage.xml \
                      --junitxml=reports/junit.xml
                '''
            }
        }

        stage('Code quality') {
            when { expression { params.RUN_SONAR } }
            steps {
                withSonarQubeEnv('sonarqube') {
                    sh 'sonar-scanner'   // reads sonar-project.properties
                }
            }
        }
    }

    post {
        always {
            junit allowEmptyResults: true, testResults: 'reports/junit.xml'
            recordCoverage(tools: [[parser: 'COBERTURA', pattern: 'coverage.xml']])
            archiveArtifacts allowEmptyArchive: true, artifacts: 'coverage.xml, reports/**'
        }
    }
}
