using Newtonsoft.Json;
using System.Collections.Generic;

namespace LightingAI.Models
{
    // Mirrors services/placer/real_placer.py → PlacedLuminaire dataclass
    public class PlacedLuminaire
    {
        [JsonProperty("id")]             public int    Id            { get; set; }
        [JsonProperty("x")]              public double X             { get; set; }
        [JsonProperty("y")]              public double Y             { get; set; }
        [JsonProperty("rotation")]       public double Rotation      { get; set; }
        [JsonProperty("lumi_type")]      public string LumiType      { get; set; } = "A";
        [JsonProperty("product_code")]   public string ProductCode   { get; set; } = "";
        [JsonProperty("description")]    public string Description   { get; set; } = "";
        [JsonProperty("wattage")]        public int    Wattage       { get; set; }
        [JsonProperty("lux_output")]     public int    LuxOutput     { get; set; }
        [JsonProperty("beam_angle_deg")] public double BeamAngleDeg  { get; set; }
        [JsonProperty("zone_type")]      public string ZoneType      { get; set; } = "";
        [JsonProperty("mounting_type")]  public string MountingType  { get; set; } = "";
        [JsonProperty("ip_rating")]      public string IpRating      { get; set; } = "IP20";
        [JsonProperty("grid_snapped")]   public bool   GridSnapped   { get; set; }
        [JsonProperty("shelf_aligned")]  public bool   ShelfAligned  { get; set; }
    }

    // Mirrors services/classifier/room_classifier_real.py → ClassifiedZone
    public class ZoneInfo
    {
        [JsonProperty("index")]      public int           Index    { get; set; }
        [JsonProperty("zone_type")]  public string        ZoneType { get; set; } = "";
        [JsonProperty("area_m2")]    public double        AreaM2   { get; set; }
        [JsonProperty("bounds")]     public List<double>  Bounds   { get; set; } = new();
        [JsonProperty("confidence")] public double        Confidence { get; set; }
        [JsonProperty("method")]     public string        Method   { get; set; } = "";
    }

    // Mirrors the JSON inside GET /jobs/{id} → result field
    public class PipelineResult
    {
        [JsonProperty("total_luminaires")] public int                  TotalLuminaires { get; set; }
        [JsonProperty("total_wattage")]    public double               TotalWattage    { get; set; }
        [JsonProperty("type_A")]           public int                  TypeA           { get; set; }
        [JsonProperty("type_B")]           public int                  TypeB           { get; set; }
        [JsonProperty("type_C")]           public int                  TypeC           { get; set; }
        [JsonProperty("type_D")]           public int                  TypeD           { get; set; }
        [JsonProperty("type_E")]           public int                  TypeE           { get; set; }
        [JsonProperty("zones")]            public List<ZoneInfo>       Zones           { get; set; } = new();
        [JsonProperty("placed")]           public List<PlacedLuminaire> Placed         { get; set; } = new();
    }

    // GET /jobs/{id} response envelope
    public class JobStatusResponse
    {
        [JsonProperty("job_id")]  public string          JobId   { get; set; } = "";
        [JsonProperty("status")]  public string          Status  { get; set; } = "";
        [JsonProperty("message")] public string          Message { get; set; } = "";
        [JsonProperty("result")]  public PipelineResult? Result  { get; set; }
    }

    // POST /process response envelope
    public class ProcessResponse
    {
        [JsonProperty("job_id")]  public string JobId   { get; set; } = "";
        [JsonProperty("status")]  public string Status  { get; set; } = "";
        [JsonProperty("message")] public string Message { get; set; } = "";
    }
}
