-- Xóa cascade nhiều tầng (auth.users → workspace → source → media_asset / extraction_revision → segment)
-- thất bại vì khóa ngoại NO ACTION từ bảng trích xuất tới media_asset/revision được kiểm tra trước khi
-- nhánh cascade sâu hơn kịp xóa dòng tham chiếu. Kiểm tra lúc commit (DEFERRED) giữ nguyên ràng buộc
-- — xóa riêng asset/revision đang được tham chiếu vẫn bị chặn — nhưng cho phép xóa cả cây trong một lệnh.

alter table public.transcript_segment drop constraint transcript_segment_audio_asset_id_workspace_id_fkey;
alter table public.transcript_segment add constraint transcript_segment_audio_asset_id_workspace_id_fkey
  foreign key (audio_asset_id, workspace_id) references public.media_asset (id, workspace_id)
  deferrable initially deferred;

alter table public.frame_region drop constraint frame_region_frame_asset_id_workspace_id_fkey;
alter table public.frame_region add constraint frame_region_frame_asset_id_workspace_id_fkey
  foreign key (frame_asset_id, workspace_id) references public.media_asset (id, workspace_id)
  deferrable initially deferred;

alter table public.frame_region drop constraint frame_region_crop_asset_id_workspace_id_fkey;
alter table public.frame_region add constraint frame_region_crop_asset_id_workspace_id_fkey
  foreign key (crop_asset_id, workspace_id) references public.media_asset (id, workspace_id)
  deferrable initially deferred;

alter table public.media_asset drop constraint media_asset_parent_asset_id_workspace_id_fkey;
alter table public.media_asset add constraint media_asset_parent_asset_id_workspace_id_fkey
  foreign key (parent_asset_id, workspace_id) references public.media_asset (id, workspace_id)
  deferrable initially deferred;

alter table public.extraction_revision drop constraint extraction_revision_parent_revision_id_workspace_id_fkey;
alter table public.extraction_revision add constraint extraction_revision_parent_revision_id_workspace_id_fkey
  foreign key (parent_revision_id, workspace_id) references public.extraction_revision (id, workspace_id)
  deferrable initially deferred;

alter table public.extraction_revision drop constraint extraction_revision_parse_revision_id_workspace_id_fkey;
alter table public.extraction_revision add constraint extraction_revision_parse_revision_id_workspace_id_fkey
  foreign key (parse_revision_id, workspace_id) references public.parse_revision (id, workspace_id)
  deferrable initially deferred;
