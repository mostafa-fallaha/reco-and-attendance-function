import azure.functions as func
from azure.functions.decorators.core import DataType
import logging, os
import json
from datetime import datetime, UTC
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from azure.ai.vision.face import FaceAdministrationClient, FaceClient
from azure.ai.vision.face.models import FaceAttributeTypeRecognition04, FaceDetectionModel, FaceRecognitionModel, QualityForRecognition
from azure.ai.vision.face.models import LargePersonGroupPerson
import pytz
import pymssql

# ─── CONFIG from ENV ─────────────────────────────────────────────────────
KEY = os.getenv("FACE_APIKEY")
ENDPOINT = os.getenv("FACE_ENDPOINT")
DB_SERVER = os.getenv("DB_SERVER")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")
DB_PORT = os.getenv("DB_PORT")
beirut_tz = pytz.timezone('Asia/Beirut')

conn = pymssql.connect(
    server=DB_SERVER,
    user=DB_USER,
    password=DB_PASSWORD,
    database=DB_NAME,
    port=DB_PORT,
    encryption='require'
)
cursor = conn.cursor()

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

@app.generic_output_binding(
    arg_name="AttendanceTable",
    type="sql",
    CommandText="dbo.Attendance",
    ConnectionStringSetting="SqlConnectionString",
    data_type=DataType.STRING
)

@app.route(route="handleAttendance", methods=["POST"])
def handleAttendance(
    req: func.HttpRequest, 
    AttendanceTable: func.Out[func.SqlRow]
) -> func.HttpResponse:
    try:
        logging.info(f"Received image upload request")
        image = req.get_body()

        cur_class = req.params.get('cur_class')

        if not image:
            return func.HttpResponse("No image provided", status_code=400)

        LARGE_PERSON_GROUP_ID = str(cur_class.lower())

        cursor.execute(
            """
            SELECT id, course_code
            FROM dbo.Schedules
            WHERE class = %s
            AND %s BETWEEN DATEADD(MINUTE, -10, session_start) AND DATEADD(MINUTE, +30, session_start)
            """,
            (cur_class, datetime.now(beirut_tz).strftime('%Y-%m-%d %H:%M:%S'))
        )
        schedules = cursor.fetchall()

        if not schedules:
            return func.HttpResponse("No Schedules Now", status_code=400)

    
        with FaceAdministrationClient(endpoint=ENDPOINT, credential=AzureKeyCredential(KEY)) as face_admin_client, \
            FaceClient(endpoint=ENDPOINT, credential=AzureKeyCredential(KEY)) as face_client:

            # Detect faces
            face_ids = []
            # We use detection model 03 to get better performance, recognition model 04 to support quality for
            # recognition attribute.

            faces = face_client.detect(
                image_content=image,
                detection_model=FaceDetectionModel.DETECTION03,
                recognition_model=FaceRecognitionModel.RECOGNITION04,
                return_face_id=True,
                return_face_attributes=[FaceAttributeTypeRecognition04.QUALITY_FOR_RECOGNITION],
            )
            

            if not faces:
                return func.HttpResponse("No faces in the image", status_code=400)
            
            if len(faces) > 1:
                return func.HttpResponse("Only one face is allowed", status_code=400)
            
            if faces[0].face_attributes.quality_for_recognition != QualityForRecognition.LOW:
                face_ids.append(faces[0].face_id)
            else:
                return func.HttpResponse("Image quality not sufficient", status_code=400)

            # Identify faces
            identify_results = face_client.identify_from_large_person_group(
                face_ids=face_ids,
                large_person_group_id=LARGE_PERSON_GROUP_ID,
            )

            if identify_results[0].candidates:

                # Verify faces
                verify_result = face_client.verify_from_large_person_group(
                    face_id=identify_results[0].face_id,
                    large_person_group_id=LARGE_PERSON_GROUP_ID,
                    person_id=identify_results[0].candidates[0].person_id,
                )
                logging.info(f"verification result: {verify_result.is_identical}. confidence: {verify_result.confidence}")

                person: LargePersonGroupPerson = face_admin_client.large_person_group.get_person(
                    large_person_group_id=LARGE_PERSON_GROUP_ID,
                    person_id=identify_results[0].candidates[0].person_id
                )

                cursor.execute(
                    """
                    SELECT *
                    FROM dbo.Attendance
                    WHERE schedule_id = %s
                    AND student_id = %s
                    """,
                    (schedules[0][0], person.name)
                )

                student_exist = cursor.fetchall()

                if student_exist:
                    return func.HttpResponse(f"Attendance for student {person.name} already taken for the schedule {schedules[0][0]}", status_code=400)
                else:
                    AttendanceTable.set(
                        func.SqlRow({
                            "schedule_id": schedules[0][0],
                            "student_id": person.name,
                            "course_code": schedules[0][1],
                            "arrival_time": datetime.now(beirut_tz).strftime('%Y-%m-%d %H:%M:%S')
                        })
                    )
                    
                logging.info(f"Student with the Id = {person.name} saved to the Attendance table")

                payload = {
                    "verification_result": verify_result.is_identical,
                    "confidence": verify_result.confidence,
                    "person": person.name
                }
                
                return func.HttpResponse(
                    body=json.dumps(payload),
                    status_code=200,
                    mimetype="application/json"
                )
            
            else:
                return func.HttpResponse(f"No person identified for face ID {identify_results[0].face_id} in image.", status_code=400)
    
    except Exception as e:
        return func.HttpResponse(f"Error: {str(e)}", status_code=400)