#!/usr/bin/env bash
set -euo pipefail
set +x

worker= bucket= size_gb= zone=fr-par-2 region= name= user=${S3_USER:-adrien}
prefixes=()
while (($#)); do
	case "$1" in
	--worker-id)
		worker=$2
		shift 2
		;;
	--bucket)
		bucket=$2
		shift 2
		;;
	--size-gb)
		size_gb=$2
		shift 2
		;;
	--name)
		name=$2
		shift 2
		;;
	--zone)
		zone=$2
		shift 2
		;;
	--region)
		region=$2
		shift 2
		;;
	--user)
		user=$2
		shift 2
		;;
	--prefix)
		prefixes+=("${2%/}")
		shift 2
		;;
	-h | --help)
		echo "Usage: $0 --worker-id ID --size-gb GB --bucket NAME --prefix PATH [--prefix PATH ...] [--zone ZONE] [--region REGION] [--user USER]"
		exit 0
		;;
	*)
		echo "Unknown option: $1" >&2
		exit 2
		;;
	esac
done

[[ -n "$worker" && -n "$bucket" && "$size_gb" =~ ^[0-9]+$ && ${#prefixes[@]} -gt 0 ]] || {
	echo 'worker-id, size-gb, bucket, and at least one prefix are required.' >&2
	exit 2
}
command -v aws >/dev/null || {
	echo 'AWS CLI is required on the worker; install awscli and ensure aws is on PATH.' >&2
	exit 1
}
for prefix in "${prefixes[@]}"; do
	[[ "$prefix" != /* ]] || {
		echo "Unsafe prefix: $prefix" >&2
		exit 2
	}
	IFS=/ read -r -a components <<<"$prefix"
	for component in "${components[@]}"; do
		[[ -n "$component" && "$component" != . && "$component" != .. ]] || {
			echo "Unsafe prefix: $prefix" >&2
			exit 2
		}
	done
done
region=${region:-${zone%-*}}
access=$(pass show services/scw/access_key)
secret=$(pass show services/scw/secret_key)
[[ -n "$access" && -n "$secret" ]] || {
	echo 'Set SCW_ACCESS_KEY for Object Storage access.' >&2
	exit 1
}
export AWS_ACCESS_KEY_ID=$access AWS_SECRET_ACCESS_KEY=$secret
[[ -z "${SCW_SESSION_TOKEN:-${AWS_SESSION_TOKEN:-}}" ]] || export AWS_SESSION_TOKEN=${SCW_SESSION_TOKEN:-$AWS_SESSION_TOKEN}

name=${name:-"prefetch-$(date +%s)-$$"}
volume= attached=0 mount_path="/mnt/$name"
find_volume_id() {
	local output json
	if ! output=$(scw block volume list "name=$name" "zone=$zone" -o json); then
		printf '%s\n' "$output" >&2
		return 1
	fi
	json=$(printf '%s\n' "$output" | awk '/^[[:space:]]*[\[{]/ {in_json=1} in_json {print}')
	if [[ -z "$json" ]]; then
		printf 'No JSON found in Scaleway volume list output:\n%s\n' "$output" >&2
		return 1
	fi
	printf '%s\n' "$json" | jq -er --arg name "$name" 'if type == "array" then . else (.volumes // []) end | map(select(.name == $name)) | if length == 1 then .[0].id else error("expected exactly one volume named \($name)") end'
}
find_data_disks() {
	local disk
	root=$(findmnt -n -o SOURCE /)
	mapfile -t root_disks < <(lsblk -s -npo NAME "$root")
	mapfile -t candidates < <(lsblk -b -dnpo NAME,SIZE,TYPE | awk -v size="$((size_gb * 1000000000))" '$2 == size && $3 == "disk" {print $1}')
	disks=()
	for disk in "${candidates[@]}"; do
		[[ " ${root_disks[*]} " == *" $disk "* ]] || disks+=("$disk")
	done
}
volume_attached_to_worker() {
	local output json
	if ! output=$(scw block volume get "$volume" "zone=$zone" -o json); then
		return 1
	fi
	json=$(printf '%s\n' "$output" | awk '/^[[:space:]]*[\[{]/ {in_json=1} in_json {print}')
	[[ -n "$json" ]] || return 1
	printf '%s\n' "$json" | jq -e --arg worker "$worker" '(.references // []) | any(.[]; .product_resource_id == $worker and .status == "attached")' >/dev/null
}
detach_volume() {
	local attempt output
	for ((attempt = 0; attempt < 15; attempt++)); do
		if output=$(scw instance server detach-volume "server-id=$worker" "volume-id=$volume" "zone=$zone" 2>&1); then
			return 0
		fi
		sleep 2
	done
	printf '%s\n' "$output" >&2
	return 1
}
run_aws_transfer() {
	local attempt delay
	for ((attempt = 1; attempt <= 5; attempt++)); do
		if "$@"; then return 0; fi
		if ((attempt < 5)); then
			delay=$((2 ** (attempt - 1)))
			echo "AWS transfer failed; retrying in ${delay}s (attempt $((attempt + 1))/5)" >&2
			sleep "$delay"
		fi
	done
	echo 'AWS transfer failed after 5 attempts.' >&2
	return 1
}
cleanup() {
	status=$?
	if mountpoint -q "$mount_path"; then sudo -n umount "$mount_path" || true; fi
	if ((attached)); then detach_volume || echo "Detach manually: $volume" >&2; fi
	if ((status)); then
		if [[ -n "$volume" ]]; then
			echo "Volume retained: $volume" >&2
		else
			echo "Volume may be retained; look up the volume named $name in zone $zone" >&2
		fi
	fi
	exit "$status"
}
trap cleanup EXIT

if volume=$(find_volume_id 2>/dev/null); then
	echo "Reusing existing volume $volume named $name"
else
	if ! create_output=$(scw block volume create "name=$name" "from-empty.size=${size_gb}GB" perf-iops=5000 "tags.0=$name" "zone=$zone" --wait -o json); then
		printf '%s\n' "$create_output" >&2
		volume=$(find_volume_id 2>/dev/null || true)
		exit 1
	fi
	volume=$(find_volume_id)
fi

if attach_output=$(scw instance server attach-volume "server-id=$worker" "volume-id=$volume" volume-type=sbs_volume "zone=$zone" 2>&1); then
	attached=1
elif volume_attached_to_worker; then
	echo "Volume $volume is already attached to worker $worker"
	attached=1
else
	printf '%s\n' "$attach_output" >&2
	exit 1
fi
for ((attempt = 0; attempt < 30; attempt++)); do
	find_data_disks
	if [[ ${#disks[@]} -eq 1 || ${#disks[@]} -gt 1 ]]; then break; fi
	sleep 2
done
[[ ${#disks[@]} -eq 1 ]] || {
	echo "Expected one new ${size_gb}GB disk after waiting 60 seconds; found ${#disks[@]}" >&2
	exit 1
}
disk=${disks[0]}
if filesystem_type=$(sudo -n blkid -p -s TYPE -o value "$disk" 2>/dev/null); then
	:
else
	blkid_status=$?
	if ((blkid_status != 2)); then
		echo "Unable to inspect filesystem on $disk (blkid exit $blkid_status); refusing to format" >&2
		exit 1
	fi
fi
if [[ -z "$filesystem_type" ]]; then
	sudo -n mkfs.ext4 -F "$disk"
elif [[ "$filesystem_type" == ext4 ]]; then
	echo "Reusing existing ext4 filesystem on $disk"
else
	echo "Refusing to use $disk with unsupported filesystem $filesystem_type" >&2
	exit 1
fi
sudo -n mkdir -p "$mount_path"
sudo -n mount "$disk" "$mount_path"
sudo -n chown "$(id -u):$(id -g)" "$mount_path"
for prefix in "${prefixes[@]}"; do
	object_key="users/$user/$prefix"
	destination="$mount_path/$prefix"
	endpoint="https://s3.$region.scw.cloud"
	if aws s3api head-object --bucket "$bucket" --key "$object_key" --endpoint-url "$endpoint" --region "$region" >/dev/null 2>&1; then
		echo "Copying s3://$bucket/$object_key to $destination"
		mkdir -p "$(dirname "$destination")"
		run_aws_transfer aws s3 cp "s3://$bucket/$object_key" "$destination" --endpoint-url "$endpoint" --region "$region"
		[[ -f "$destination" ]] || {
			echo "Requested S3 object was not copied: $object_key" >&2
			exit 1
		}
	else
		source="s3://$bucket/$object_key/"
		echo "Syncing $source to $destination"
		mkdir -p "$destination"
		run_aws_transfer aws s3 sync "$source" "$destination" --endpoint-url "$endpoint" --region "$region"
		find "$destination" -type f -print -quit | grep -q . || {
			echo "Empty prefix: $prefix" >&2
			exit 1
		}
	fi
done
sync
filesystem_uuid=$(sudo -n blkid -s UUID -o value "$disk")
sudo -n umount "$mount_path"
detach_volume
attached=0
trap - EXIT
echo "Ready: $volume"
echo "FILESYSTEM_UUID=$filesystem_uuid"
