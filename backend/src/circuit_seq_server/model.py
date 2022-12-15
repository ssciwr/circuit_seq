from __future__ import annotations
from typing import Optional, Dict, Tuple, List
import smtplib
from email.message import EmailMessage
import re
import flask
import zipfile
import shutil
import argon2
import tempfile
import pathlib
import datetime
from flask_sqlalchemy import SQLAlchemy
from werkzeug.datastructures import FileStorage
from dataclasses import dataclass
from circuit_seq_server.logger import get_logger
from circuit_seq_server.utils import get_primary_key
from circuit_seq_server.utils import get_start_of_week
from circuit_seq_server.utils import parse_seq_to_fasta
import csv
from circuit_seq_server.utils import encode_activation_token, decode_activation_token

db = SQLAlchemy()
ph = argon2.PasswordHasher()
logger = get_logger("CircuitSeqServer")


@dataclass
class Settings(db.Model):
    id: int = db.Column(db.Integer, primary_key=True)
    datetime: datetime.datetime = db.Column(db.DateTime, nullable=False)
    email: str = db.Column(db.String(256), nullable=False)
    settings_dict: Dict = db.Column(db.PickleType, nullable=False)


def default_settings_dict() -> Dict:
    return {
        "plate_n_rows": 8,
        "plate_n_cols": 12,
        "running_options": ["dna_r9.4.1_450bps_sup.cfg", "dna_r9.4.1_480bps_sup.cfg"],
        "last_submission_day": 3,
    }


def get_current_settings() -> Dict:
    settings_tuple = db.session.execute(
        db.select(Settings).order_by(db.desc(Settings.id))
    ).first()
    if settings_tuple is not None:
        settings_dict = settings_tuple[0].settings_dict
        for key, value in default_settings_dict().items():
            # use default values for any missing keys
            if key not in settings_dict:
                settings_dict[key] = value
        return settings_dict
    # no settings in db: create default settings and add to db
    settings = Settings(
        datetime=datetime.datetime.today(),
        email="default",
        settings_dict=default_settings_dict(),
    )
    db.session.add(settings)
    db.session.commit()
    return settings.settings_dict


def set_current_settings(email: str, settings_dict: Dict) -> Tuple[str, int]:
    for required_field in default_settings_dict():
        if required_field not in settings_dict:
            return (
                f"Required field {required_field} missing - settings not updated",
                401,
            )
    settings = Settings(
        datetime=datetime.datetime.today(), email=email, settings_dict=settings_dict
    )
    db.session.add(settings)
    db.session.commit()
    return f"Settings updated by {settings.email} at {settings.datetime}", 200


@dataclass
class Sample(db.Model):
    id: int = db.Column(db.Integer, primary_key=True)
    email: str = db.Column(db.String(256), nullable=False)
    primary_key: str = db.Column(db.String(32), nullable=False, unique=True)
    name: str = db.Column(db.String(128), nullable=False)
    running_option: str = db.Column(db.String(128), nullable=False)
    concentration: int = db.Column(db.Integer, nullable=False)
    reference_sequence_description: Optional[str] = db.Column(
        db.String(256), nullable=True
    )
    date: datetime.date = db.Column(db.Date, nullable=False)
    has_results_fasta: bool = db.Column(db.Boolean, nullable=False)
    has_results_gbk: bool = db.Column(db.Boolean, nullable=False)
    has_results_zip: bool = db.Column(db.Boolean, nullable=False)

    def results_dir(self) -> str:
        return f"{_get_basepath(self.date)}/results"

    def results_file_path(self, filetype: str) -> str:
        return f"{self.results_dir()}/{self.primary_key}_{self.name}.{filetype}"


def _samples_this_week(current_date: datetime.date):
    start_of_week = get_start_of_week(current_date)
    return (
        db.session.execute(
            db.select(Sample)
            .filter(Sample.date >= start_of_week)
            .filter(Sample.date < start_of_week + datetime.timedelta(weeks=1))
        )
        .scalars()
        .all()
    )


def _count_samples_this_week(current_date: datetime.date) -> int:
    return len(_samples_this_week(current_date))


def remaining_samples_this_week(
    current_date: Optional[datetime.date] = None,
) -> Dict:
    if current_date is None:
        current_date = datetime.date.today()
    settings = get_current_settings()
    year, week, day = current_date.isocalendar()
    message = ""
    max_samples = settings["plate_n_rows"] * settings["plate_n_cols"]
    remaining = max_samples - _count_samples_this_week(current_date)
    if day > settings["last_submission_day"]:
        remaining = 0
        message = "Sample submission is closed for this week."
    elif remaining == 0:
        message = "All samples have been taken this week."
    return {"remaining": remaining, "message": message}


def _get_basepath(current_date: datetime.date) -> str:
    year, week, _ = current_date.isocalendar()
    data_path = flask.current_app.config["CIRCUITSEQ_DATA_PATH"]
    return f"{data_path}/{year}/{week}"


def _write_samples_as_tsv_this_week(current_date: datetime.date) -> None:
    filename = f"{_get_basepath(current_date)}/inputs/samples.tsv"
    logger.info(f"Generating {filename}")
    with open(filename, "w", newline="") as tsv_file:
        writer = csv.writer(tsv_file, delimiter="\t", lineterminator="\n")
        columns = [
            "date",
            "primary_key",
            "email",
            "name",
            "running_option",
            "concentration",
        ]
        writer.writerow(columns)
        for sample in _samples_this_week(current_date):
            logger.info(f"  - {sample.primary_key}")
            writer.writerow([getattr(sample, column) for column in columns])


def update_samples_zipfile(current_date: Optional[datetime.date] = None) -> str:
    if current_date is None:
        current_date = datetime.date.today()
    base_path = _get_basepath(current_date)
    inputs_dir = f"{base_path}/inputs"
    pathlib.Path(inputs_dir).mkdir(parents=True, exist_ok=True)
    _write_samples_as_tsv_this_week(data_path, current_date)
    logger.info(f"Creating zip file of {inputs_dir}..")
    zip_filename = shutil.make_archive(
        base_name=f"{base_path}/samples",
        format="zip",
        root_dir=f"{base_path}/inputs",
    )
    logger.info(f"  -> created zip file {zip_filename}")
    return zip_filename


def get_samples(email: Optional[str] = None) -> Dict[str, List[Sample]]:
    samples = {}
    start_of_week = get_start_of_week()
    selected_samples = db.select(Sample).order_by(db.desc("date"))
    if email is not None:
        selected_samples = selected_samples.filter(Sample.email == email)
    samples["current_samples"] = (
        db.session.execute(selected_samples.filter(Sample.date >= start_of_week))
        .scalars()
        .all()
    )
    samples["previous_samples"] = (
        db.session.execute(selected_samples.filter(Sample.date < start_of_week))
        .scalars()
        .all()
    )
    return samples


def _send_result_email(sample: Sample, success: bool) -> Tuple[str, int]:
    message_head = (
        f"Dear {sample.email},\n\n"
        f"Your sample {sample.primary_key}_{sample.name} has been processed"
    )
    if success:
        message_body = (
            f", however no correct de-novo assembly has been determined.\n\n"
            f"Please consider handing in this sample next week again.\n\n"
        )
    else:
        message_body = (
            f" and the results are attached to this email.\n\n"
            f"You can also download the full analysis data by "
            f"logging in to your account at https://circuitseq.iwr.uni-heidelberg.de\n\n"
        )
    message_tail = (
        "Best wishes,\n\nCircuitSEQ Team.\nhttps://circuitseq.iwr.uni-heidelberg.de"
    )
    try:
        logger.info(
            f"Sending {sample.primary_key} success={success} result email to {sample.email}"
        )
        msg = EmailMessage()
        msg.set_content(f"{message_head}{message_body}{message_tail}")
        msg[
            "Subject"
        ] = f"CircuitSEQ results for sample {sample.primary_key}_{sample.name}"
        msg["From"] = "no-reply@circuitseq.iwr.uni-heidelberg.de"
        msg["To"] = sample.email
        if success is True:
            for filetype in ["fasta", "gbk"]:
                result_file = sample.results_file_path(filetype)
                with open(result_file, "rb") as fp:
                    result_file_data = fp.read()
                msg.add_attachment(
                    result_file_data,
                    maintype="application",
                    subtype="octet-stream",
                    filename=result_file.split("/")[-1],
                )
        with smtplib.SMTP("email:587") as s:
            s.send_message(msg)
    except Exception as e:
        logger.warning(f"  --> failed to send result email: {e}")
        return (
            f"Failed to send results email for {sample.primary_key} to {sample.email}: {e}",
            401,
        )
    return (
        f"Results email for {sample.primary_key} sent to {sample.email}",
        200,
    )


def _is_valid_filename(primary_key: str, filename: str) -> bool:
    segments = pathlib.Path(filename).name.split("_")
    if len(segments) < 3:
        return False
    yy = segments[0]
    ww = segments[1]
    nn = segments[2]
    return primary_key == f"{yy}_{ww}_{nn}"


def process_result(
    primary_key: str, success: bool, result_zip_file: Optional[FileStorage]
) -> Tuple[str, int]:
    sample = db.session.execute(
        db.select(Sample).filter_by(primary_key=primary_key)
    ).scalar_one_or_none()
    if sample is None:
        logger.warning(f" --> Unknown primary key {key}")
        return f"Unknown primary key {primary_key}", 401
    if success is False:
        logger.info("Sending result failure message for {primary_key}")
        return _send_result_email(sample, success)
    if not _is_valid_filename(primary_key, result_zip_file.filename):
        return (
            f"Zip filename {result_zip_file.filename} doesn't match key {primary_key}",
            401,
        )
    logger.info(f"Processing zip file {result_zip_file} for {primary_key}")
    results_dir = sample.results_dir()
    pathlib.Path(results_dir).mkdir(parents=True, exist_ok=True)
    basename = f"{key}_{sample.name}"
    results_file = pathlib.Path(results_dir) / f"{basename}.zip"
    result_zip_file.save(results_file)
    logger.info(f"  --> {results_file}")
    try:
        zip_file = zipfile.ZipFile(results_file)
        for zip_info in zip_file.infolist():
            if not zip_info.is_dir():
                # remove any leading directories from filenames in zip file
                zip_info.filename = pathlib.Path(zip_info.filename).name
        zip_filenames = [f.filename for f in zip_file.filelist]
        for filetype in ["fasta", "gbk"]:
            filename = f"{basename}.{filetype}"
            if filename not in zip_filenames:
                logger.warning(f"{filename} not in {zip_filenames}")
                return (
                    f"Zip file does not contain {filename}",
                    401,
                )
            zip_file.extract(filename, results_dir)
            logger.info(f"  --> {results_dir}/{filename}")
    except Exception as e:
        logger.warning(f"Failed to process zip file: {e}")
        return (
            f"Failed to process zip file: {e}",
            401,
        )
    sample.has_results_fasta = True
    sample.has_results_gbk = True
    sample.has_results_zip = True
    db.session.commit()
    email_message, _ = _send_result_email(sample, success, files_to_email)
    return f"{results_file} saved, {email_message}", 200


@dataclass
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.Text, nullable=False, unique=True)
    password_hash = db.Column(db.Text, nullable=False)
    activated = db.Column(db.Boolean, nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False)

    def set_password(self, current_password: str, new_password: str) -> bool:
        if self.check_password(current_password):
            self.password_hash = ph.hash(new_password)
            db.session.commit()
            return True
        return False

    def check_password(self, password: str) -> bool:
        try:
            ph.verify(self.password_hash, password)
        except argon2.exceptions.VerificationError:
            return False
        if ph.check_needs_rehash(self.password_hash):
            self.password_hash = ph.hash(password)
            db.session.commit()
        return True

    def as_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "activated": self.activated,
            "is_admin": self.is_admin,
        }


def is_valid_email(email: str) -> bool:
    return re.match(r"\S+@((\S*heidelberg)|embl|dkfz)\.de$", email) is not None


def is_valid_password(password: str) -> bool:
    return re.match(r"^(?=.*[A-Z])(?=.*[a-z])(?=.*[0-9]).{8,}$", password) is not None


def _send_activation_email(email: str):
    secret_key = flask.current_app.config["JWT_SECRET_KEY"]
    token = encode_activation_token(email, secret_key)
    url = f"https://circuitseq.iwr.uni-heidelberg.de/activate/{token}"
    logger.info(f"Activation url: {url}")
    msg = EmailMessage()
    msg.set_content(
        f"To activate your CircuitSEQ account, please confirm your email address by clicking on the following link:\n\n{url}"
    )
    msg["Subject"] = "CircuitSEQ account activation"
    msg["From"] = "no-reply@circuitseq.iwr.uni-heidelberg.de"
    msg["To"] = email
    with smtplib.SMTP("email:587") as s:
        s.send_message(msg)


def add_new_user(email: str, password: str, is_admin: bool) -> Tuple[str, int]:
    if not is_valid_email(email):
        return "Please use a uni-heidelberg, dkfz or embl email address.", 401
    if not is_valid_password(password):
        return (
            "Password must contain at least 8 characters, including lower-case, upper-case and a number",
            401,
        )
    if (
        db.session.execute(
            db.select(User).filter(User.email == email)
        ).scalar_one_or_none()
        is not None
    ):
        return (
            "This email address is already in use",
            401,
        )
    try:
        _send_activation_email(email)
    except Exception as e:
        logger.warning(f"Send activation email failed: {e}")
        return "Failed to send activation email", 401
    try:
        db.session.add(
            User(
                email=email,
                password_hash=ph.hash(password),
                activated=False,
                is_admin=is_admin,
            )
        )
        db.session.commit()
    except Exception as e:
        logger.warning(f"Error adding user to db: {e}")
        return "Failed to create new user", 401
    return (
        f"Successful signup for {email}. To activate your account, please click on the link in the activation email from no-reply@circuitseq.iwr.uni-heidelberg.de sent to this email address",
        200,
    )


def activate_user(token: str) -> Tuple[str, int]:
    logger.info(f"Activate request for token {token}")
    secret_key = flask.current_app.config["JWT_SECRET_KEY"]
    email = decode_activation_token(token, secret_key)
    if email is None:
        return "Invalid or expired activation link", 401
    logger.info(f"  -> email '{email}'")
    user = db.session.execute(
        db.select(User).filter(User.email == email)
    ).scalar_one_or_none()
    if user is None:
        logger.info(f"  -> Unknown email address '{email}'")
        return f"Unknown email address {email}", 401
    if user.activated is True:
        logger.info(f"  -> user with email {email} already activated")
        return f"Account for {email} is already activated", 401
    user.activated = True
    db.session.commit()
    return f"Account {email} activated", 200


def add_new_sample(
    email: str,
    name: str,
    running_option: str,
    concentration: int,
    reference_sequence_file: Optional[FileStorage],
) -> Tuple[Optional[Sample], str]:
    today = datetime.date.today()
    base_path = _get_basepath(today)
    year, week, day = today.isocalendar()
    count = _count_samples_this_week(today)
    settings = get_current_settings()
    remaining_samples = remaining_samples_this_week()
    if remaining_samples["remaining"] == 0:
        return None, remaining_samples["message"]
    key = get_primary_key(
        year=year,
        week=week,
        current_count=count,
        n_rows=settings["plate_n_rows"],
        n_cols=settings["plate_n_cols"],
    )
    if key is None:
        return None, "No more samples left this week."
    reference_sequence_description: Optional[str] = None
    ref_seq_dir = pathlib.Path(f"{base_path}/inputs/references")
    ref_seq_dir.mkdir(parents=True, exist_ok=True)
    if reference_sequence_file is not None:
        ref_seq_filename = str(ref_seq_dir / f"{key}_{name}.fasta")
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_file = pathlib.Path(tmp_dir) / "temp.txt"
            logger.info(
                f"Saving {reference_sequence_file.filename} to temporary file {temp_file}"
            )
            reference_sequence_file.save(temp_file)
            reference_sequence_description = parse_seq_to_fasta(
                temp_file, ref_seq_filename, reference_sequence_file.filename
            )
            if reference_sequence_description is None:
                return None, "Failed to parse reference sequence file."
    new_sample = Sample(
        email=email,
        primary_key=key,
        name=name,
        running_option=running_option,
        concentration=concentration,
        reference_sequence_description=reference_sequence_description,
        date=today,
        has_results_zip=False,
        has_results_fasta=False,
        has_results_gbk=False,
    )
    db.session.add(new_sample)
    db.session.commit()
    return new_sample, ""
